"""Expose only inference and model inspection on one operator-selected Ollama."""

import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import socket
import ssl
from urllib.parse import urlsplit


PATHS = {
    "/api/tags",
    "/api/show",
    "/api/chat",
    "/api/generate",
    "/v1/models",
    "/v1/chat/completions",
    "/v1/completions",
}


def external_endpoint(value):
    if (
        not isinstance(value, str)
        or len(value) > 1000
        or any(char.isspace() for char in value)
    ):
        raise ValueError("Enter an external Ollama HTTP(S) URL.")
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/", "/api", "/api/", "/v1", "/v1/"}
    ):
        raise ValueError(
            "Use an external Ollama origin without credentials, query or fragment."
        )
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if not 1 <= port <= 65535 or parsed.hostname in {
        "app",
        "postgres",
        "runner",
        "worker",
        "vibearr",
        "localhost",
        "runner-ollama",
    }:
        raise ValueError("Ollama must be an explicitly provided external service.")
    try:
        address = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        address = None
    if address and (
        address.is_loopback
        or address.is_link_local
        or address.is_unspecified
        or address.is_multicast
    ):
        raise ValueError(
            "Loopback, metadata and special-address endpoints are forbidden."
        )
    return parsed.scheme + "://" + parsed.netloc


def resolve_endpoint(value):
    parsed = urlsplit(external_endpoint(value))
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    addresses = sorted(
        {
            entry[4][0]
            for entry in socket.getaddrinfo(
                parsed.hostname, port, socket.AF_INET, socket.SOCK_STREAM
            )
        }
    )
    for item in addresses:
        address = ipaddress.ip_address(item)
        if (
            address.is_loopback
            or address.is_link_local
            or address.is_unspecified
            or address.is_multicast
        ):
            raise ValueError("External Ollama resolved to a prohibited address.")
    if not addresses:
        raise ValueError("External Ollama did not resolve.")
    return parsed, addresses[0], port


class Relay(ThreadingHTTPServer):
    daemon_threads = True
    endpoint = ""
    model = ""
    select_connection = None
    denied: set[str] = set()


class Handler(BaseHTTPRequestHandler):
    server: Relay

    def log_message(self, *_args):
        pass

    def do_GET(self):
        self.forward()

    def do_POST(self):
        self.forward()

    def forward(self):
        connection = None
        started = False
        try:
            self.connection.settimeout(30)
            if (
                self.path not in PATHS
                or (not self.server.endpoint and self.server.select_connection is None)
                or self.headers.get("Transfer-Encoding")
            ):
                raise ValueError
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 <= length <= 16 * 1024 * 1024:
                raise ValueError
            body = self.rfile.read(length)
            if len(body) != length:
                raise ValueError
            model = self.server.model
            endpoint = self.server.endpoint
            payload = None
            if self.command == "POST":
                payload = json.loads(body)
                if not isinstance(payload, dict):
                    raise ValueError("Use the selected installed Ollama model.")
            token = self.headers.get("X-Vibearr-Grant", "")
            if self.server.select_connection is not None:
                selected = self.server.select_connection(
                    token, payload.get("model") if payload else None
                )
                endpoint, model = selected["endpoint"], selected["model"]
            if payload is not None and payload.get("model") != model:
                raise ValueError("Use the selected installed Ollama model.")
            parsed, address, port = resolve_endpoint(endpoint)
            if address in self.server.denied:
                raise ValueError(
                    "External Ollama resolved to an internal control service."
                )
            if parsed.scheme == "https":
                connection = http.client.HTTPSConnection(
                    parsed.hostname, port, timeout=30
                )
                connection.sock = ssl.create_default_context().wrap_socket(
                    socket.create_connection((address, port), timeout=30),
                    server_hostname=parsed.hostname,
                )
            else:
                connection = http.client.HTTPConnection(address, port, timeout=30)
            connection.request(
                self.command,
                self.path,
                body=body or None,
                headers={"Host": parsed.netloc, "Content-Type": "application/json"},
            )
            response = connection.getresponse()
            if 300 <= response.status < 400:
                raise ValueError
            self.send_response(response.status)
            started = True
            self.send_header(
                "Content-Type", response.getheader("Content-Type", "application/json")
            )
            self.send_header("Connection", "close")
            self.end_headers()
            received = 0
            while True:
                if self.server.select_connection is not None:
                    self.server.select_connection(token, model)
                chunk = response.read1(65536)
                if not chunk:
                    break
                if self.server.select_connection is not None:
                    self.server.select_connection(token, model)
                received += len(chunk)
                if received > 64 * 1024 * 1024:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
        except (OSError, ValueError, http.client.HTTPException):
            if not started:
                self.send_error(
                    503,
                    "Selected external Ollama is unavailable or the operation is unsupported.",
                )
        finally:
            if connection is not None:
                connection.close()
            self.close_connection = True
