import base64
import hmac
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .contracts import (
    MAX_ARCHIVE,
    ArtifactRemoved,
    Conflict,
    NotFound,
    Rejected,
    canonical,
)
from .client import ExecutorUnavailable, ExecutorRejected


class Server(ThreadingHTTPServer):
    daemon_threads = True
    runtime: Any
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
        except ExecutorRejected as exc:
            code = exc.http_status if exc.http_status in {400, 404, 409, 410} else 409
            self.reply(
                code,
                {
                    "error": "Worker rejected the bound operation; inspect its current status."
                },
            )
        except ExecutorUnavailable:
            self.reply(
                503,
                {
                    "error": "Worker control is unavailable; reconcile the existing operation."
                },
            )
        except (ValueError, TypeError, KeyError, UnicodeError):
            self.reply(
                400, {"error": "Request violates the versioned executor contract."}
            )
        except OSError as exc:
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
            return runtime.capabilities()
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
        if self.path == "/v2/dependencies/prepare" and hasattr(
            runtime, "dependency_artifacts"
        ):
            return runtime.dependency_artifacts.prepare(data)
        artifact_parts = self.path.split("/")
        if (
            len(artifact_parts) == 5
            and artifact_parts[:3] == ["", "v2", "dependencies"]
            and hasattr(runtime, "dependency_artifacts")
        ):
            identity, action = artifact_parts[3:]
            if action == "chunks" and set(data) == {"offset", "content"}:
                content = base64.b64decode(data["content"], validate=True)
                return runtime.dependency_artifacts.append(
                    identity, data["offset"], content
                )
            if action == "purge" and set(data) == {"run_id"}:
                return runtime.dependency_artifacts.purge(identity, data["run_id"])
        if (
            len(artifact_parts) == 5
            and artifact_parts[:3] == ["", "v2", "operations"]
            and artifact_parts[4] == "dependencies"
            and set(data) == {"offset", "limit"}
            and hasattr(runtime, "dependency_chunk")
        ):
            content = runtime.dependency_chunk(
                artifact_parts[3], data["offset"], data["limit"]
            )
            return {
                "content": base64.b64encode(content).decode(),
                "next_offset": data["offset"] + len(content),
            }
        if not hasattr(runtime, "dependency_builds") and self.path.startswith(
            "/v1/dependency-builds/"
        ):
            raise Rejected(
                "This runtime does not implement legacy image-building operations."
            )
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
