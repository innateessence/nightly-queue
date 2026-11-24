import ipaddress
import base64
import json
import select
import socket
import socketserver
import sys
import time
from pathlib import Path

from .contracts import public_host


def resolve_public(host: str, denied: set[str]) -> list[str]:
    addresses = sorted(
        {
            entry[4][0]
            for entry in socket.getaddrinfo(
                host, 443, socket.AF_INET, socket.SOCK_STREAM
            )
        }
    )
    if not addresses or any(
        not ipaddress.ip_address(address).is_global
        or ipaddress.ip_address(address).is_multicast
        or address in denied
        for address in addresses
    ):
        raise ValueError("Destination is not public.")
    return addresses


class Proxy(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
    hosts: set[str]
    denied: set[str]
    authorize = None


class Handler(socketserver.StreamRequestHandler):
    server: Proxy

    def handle(self):
        self.connection.settimeout(10)
        try:
            line = self.rfile.readline(4097)
            if len(line) > 4096:
                raise ValueError("Request is too large.")
            method, destination, version = line.decode("ascii").strip().split(" ")
            host, port = destination.rsplit(":", 1)
            if method != "CONNECT" or port != "443" or version != "HTTP/1.1":
                raise ValueError("Only HTTPS CONNECT is allowed.")
            if not public_host(host) or host not in self.server.hosts:
                raise ValueError("Destination is not allowed.")
            size = 0
            credentials = []
            while True:
                header = self.rfile.readline(4097)
                size += len(header)
                if size > 16384 or not header:
                    raise ValueError("Invalid headers.")
                if header == b"\r\n":
                    break
                if header.lower().startswith(b"proxy-authorization:"):
                    credentials.append(header.split(b":", 1)[1].strip().decode("ascii"))
            token = ""
            if self.server.authorize is not None:
                if len(credentials) != 1 or not credentials[0].startswith("Basic "):
                    raise ValueError("Operation grant is required.")
                credential = base64.b64decode(credentials[0][6:], validate=True).decode(
                    "ascii"
                )
                user, separator, token = credential.partition(":")
                if user != "grant" or not separator:
                    raise ValueError("Invalid operation grant.")
                self.server.authorize(token, host)
            addresses = resolve_public(host, self.server.denied)
            # Connect to the validated numeric address; never resolve again during connect.
            with socket.create_connection((addresses[0], 443), timeout=15) as upstream:
                self.wfile.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                self.wfile.flush()
                deadline = time.monotonic() + 3600
                while time.monotonic() < deadline:
                    if self.server.authorize is not None:
                        self.server.authorize(token, host)
                    readable, _, _ = select.select(
                        [self.connection, upstream], [], [], 1
                    )
                    for stream in readable:
                        content = stream.recv(65536)
                        if not content:
                            return
                        if self.server.authorize is not None:
                            self.server.authorize(token, host)
                        (
                            upstream if stream is self.connection else self.connection
                        ).sendall(content)
        except (ValueError, OSError, UnicodeError):
            try:
                self.wfile.write(
                    b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
                )
            except OSError:
                pass


def main():
    policy = json.loads(Path(sys.argv[1]).read_bytes())
    with Proxy(("0.0.0.0", 3128), Handler) as server:
        server.hosts = set(policy["hosts"])
        server.denied = set(policy["denied"])
        server.serve_forever()


if __name__ == "__main__":
    main()
