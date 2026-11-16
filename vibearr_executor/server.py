import argparse
import base64
import hmac
import ipaddress
import json
import signal
import ssl
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .contracts import (
    MAX_ARCHIVE,
    ArtifactRemoved,
    Conflict,
    NotFound,
    Profile,
    Rejected,
    canonical,
)
from .engine import EngineError
from .runtime import Runtime


class Server(ThreadingHTTPServer):
    daemon_threads = True
    runtime: Runtime
    token: str


class Handler(BaseHTTPRequestHandler):
    server: Server
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):
        pass

    def setup(self):
        super().setup()
        self.connection.settimeout(15)

    def reply(self, code: int, value: dict):
        content = canonical(value)
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(content)

    def do_POST(self):
        self.dispatch()

    def do_GET(self):
        self.dispatch()

    def dispatch(self):
        supplied = self.headers.get("Authorization", "")
        if not hmac.compare_digest(
            supplied.encode(), ("Bearer " + self.server.token).encode()
        ):
            self.reply(401, {"error": "Executor authentication required."})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if (
                length < 0
                or length > MAX_ARCHIVE * 4 // 3 + 65536
                or self.headers.get("Transfer-Encoding")
            ):
                raise Rejected("Invalid request body length.")
            content = self.rfile.read(length)
            if len(content) != length:
                raise Rejected("Truncated request body.")
            data = json.loads(content) if content else {}
            if not isinstance(data, dict):
                raise Rejected("Supply a JSON object.")
            self.reply(200, self.route(data))
        except NotFound as exc:
            self.reply(404, {"error": str(exc)})
        except ArtifactRemoved as exc:
            self.reply(410, {"error": str(exc)})
        except Conflict as exc:
            self.reply(409, {"error": str(exc)})
        except Rejected as exc:
            self.reply(400, {"error": str(exc)})
        except (ValueError, TypeError, KeyError, UnicodeError):
            self.reply(
                400, {"error": "Request violates the versioned executor contract."}
            )
        except (EngineError, OSError) as exc:
            print(
                type(exc).__name__ + ": " + str(exc)[:1500], file=sys.stderr, flush=True
            )
            self.reply(
                503,
                {
                    "error": "Executor state is uncertain; reconcile status before retrying. Inspect private service diagnostics."
                },
            )

    def route(self, data: dict) -> dict:
        runtime = self.server.runtime
        if self.command == "GET" and self.path == "/v1/capabilities" and not data:
            from .qualification import capabilities

            return capabilities(runtime)
        if self.command != "POST":
            raise Rejected("Unsupported endpoint.")
        if self.path == "/v1/artifacts" and set(data) == {"kind", "sha256", "content"}:
            return runtime.upload(
                data["kind"],
                base64.b64decode(data["content"], validate=True),
                data["sha256"],
            )
        if self.path == "/v1/prepare":
            return runtime.prepare(data)
        if self.path == "/v1/dependency-builds/prepare":
            return runtime.dependency_builds.prepare(data)
        parts = self.path.split("/")
        if (
            len(parts) == 5
            and parts[:3] == ["", "v1", "dependency-builds"]
            and not data
            and parts[4] in {"start", "status", "stop", "pause", "resume"}
        ):
            return getattr(runtime.dependency_builds, parts[4])(parts[3])
        if (
            len(parts) == 5
            and parts[:3] == ["", "v1", "dependency-builds"]
            and parts[4] == "purge"
        ):
            if set(data) == {"run_id", "resources"}:
                return runtime.dependency_builds.purge(
                    parts[3], data["run_id"], data["resources"]
                )
            raise Rejected("Supply the expected run and retention resources.")
        if len(parts) != 5 or parts[:3] != ["", "v1", "operations"]:
            raise Rejected("Unsupported endpoint.")
        operation_id, action = parts[3:]
        if action == "secret" and set(data) == {"secret"}:
            return runtime.supply_secret(operation_id, data["secret"])
        if (
            action
            in {"start", "status", "stop", "pause", "resume", "termination_receipt"}
            and not data
        ):
            return getattr(runtime, action)(operation_id)
        if action == "purge" and set(data) == {"run_id", "resources"}:
            return runtime.purge(operation_id, data["run_id"], data["resources"])
        if action == "logs" and set(data) == {"offset", "limit"}:
            value = runtime.logs(operation_id, data["offset"], data["limit"])
            return {
                "offset": data["offset"],
                "next_offset": data["offset"] + len(value),
                "content": base64.b64encode(value).decode(),
            }
        if action == "result" and not data:
            manifest, source = runtime.export(operation_id)
            return {"manifest": manifest, "content": base64.b64encode(source).decode()}
        raise Rejected("Unsupported endpoint or fields.")


def monitor(runtime: Runtime, stop: threading.Event):
    while not stop.wait(1):
        try:
            runtime.dependency_builds.enforce_controls()
        except (Rejected, EngineError, OSError, ValueError):
            pass
        for path in runtime.operations.iterdir():
            try:
                runtime.status(path.name)
            except (Rejected, EngineError, OSError, ValueError):
                continue


def main():
    parser = argparse.ArgumentParser(
        description="Trusted OCI executor; run on a dedicated Linux Docker host."
    )
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--profiles", type=Path, required=True)
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8791)
    parser.add_argument("--tls-cert", type=Path)
    parser.add_argument("--tls-key", type=Path)
    parser.add_argument("--deny-ip", action="append", default=[])
    args = parser.parse_args()
    if args.token_file.stat().st_mode & 0o077:
        parser.error("Token file must be private (mode 0600).")
    token = args.token_file.read_text().strip()
    if len(token) < 32:
        parser.error("Executor token must contain at least 32 random characters.")
    if not ipaddress.ip_address(args.bind).is_loopback and not (
        args.tls_cert and args.tls_key
    ):
        parser.error("Remote executor control requires TLS.")
    profiles = [
        Profile.parse(value) for value in json.loads(args.profiles.read_bytes())
    ]
    runtime = Runtime(
        args.state,
        profiles,
        denied_ips=tuple(args.deny_ip),
    )
    stop = threading.Event()
    with Server((args.bind, args.port), Handler) as server:
        server.runtime, server.token = runtime, token
        if args.tls_cert and args.tls_key:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(args.tls_cert, args.tls_key)
            server.socket = context.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(target=monitor, args=(runtime, stop), daemon=True)
        thread.start()

        def shutdown(_signum, _frame):
            threading.Thread(target=server.shutdown, daemon=True).start()

        signal.signal(signal.SIGTERM, shutdown)
        signal.signal(signal.SIGINT, shutdown)
        try:
            server.serve_forever()
        finally:
            stop.set()
            thread.join(timeout=60)
            runtime.close()


if __name__ == "__main__":
    main()
