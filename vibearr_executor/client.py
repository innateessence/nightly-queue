import base64
import ipaddress
import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .artifacts import result_manifest
from .contracts import MAX_ARCHIVE, MAX_RESULT, Rejected, canonical, digest, identifier


class ExecutorUnavailable(RuntimeError):
    def __init__(self, message: str, *, http_status: int | None = None):
        super().__init__(message)
        self.http_status = http_status


class ExecutorRejected(ExecutorUnavailable):
    pass


class ExecutorArtifactRemoved(ExecutorRejected):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        raise ExecutorUnavailable("Executor redirects are forbidden.")


class Client:
    def __init__(self, url: str, token: str, *, timeout: int = 90):
        parsed = urllib.parse.urlsplit(url)
        try:
            loopback = ipaddress.ip_address(parsed.hostname or "").is_loopback
        except ValueError:
            loopback = parsed.hostname == "localhost"
        if parsed.scheme not in {"http", "https"} or (
            parsed.scheme == "http" and not loopback
        ):
            raise Rejected("Use HTTPS or loopback executor control.")
        if (
            parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise Rejected("Supply only an executor origin URL.")
        if len(token) < 32:
            raise Rejected("Executor control token is missing.")
        self.url, self.token, self.timeout = url.rstrip("/"), token, timeout
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), NoRedirect()
        )

    @classmethod
    def from_environment(cls):
        import os

        if not os.environ.get("VIBEARR_EXECUTOR_URL") or not os.environ.get(
            "VIBEARR_EXECUTOR_TOKEN_FILE"
        ):
            raise Rejected(
                "Set VIBEARR_EXECUTOR_URL and VIBEARR_EXECUTOR_TOKEN_FILE for authenticated executor control."
            )
        path = Path(os.environ["VIBEARR_EXECUTOR_TOKEN_FILE"])
        if path.stat().st_mode & 0o077:
            raise Rejected("Executor token file must be private.")
        return cls(os.environ["VIBEARR_EXECUTOR_URL"], path.read_text().strip())

    def request(self, path: str, data: dict | None = None) -> dict:
        headers = {
            "Authorization": "Bearer " + self.token,
            "Content-Type": "application/json",
        }
        request = urllib.request.Request(
            self.url + path,
            data=canonical(data) if data is not None else None,
            headers=headers,
        )
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                content = response.read(MAX_ARCHIVE * 4 // 3 + MAX_RESULT + 65536)
                if response.read(1):
                    raise ExecutorUnavailable("Executor response exceeds byte limit.")
                value = json.loads(content)
                if not isinstance(value, dict):
                    raise ExecutorUnavailable("Invalid executor response.")
                return value
        except urllib.error.HTTPError as exc:
            if exc.code == 410:
                raise ExecutorArtifactRemoved(
                    "Executor artifact was removed by retention.", http_status=410
                ) from exc
            error = ExecutorRejected if 400 <= exc.code < 500 else ExecutorUnavailable
            raise error(
                (
                    "Executor rejected the request."
                    if exc.code < 500
                    else "Executor operation is uncertain; reconcile status."
                ),
                http_status=exc.code,
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            raise ExecutorUnavailable(
                "Executor request failed; reconcile operation status before any retry."
            ) from exc

    def capabilities(self) -> dict:
        return self.request("/v1/capabilities")

    def prepare_dependencies(self, manifest: dict) -> dict:
        return self.request("/v1/dependency-builds/prepare", manifest)

    def dependencies(self, build_id: str, action: str) -> dict:
        identifier(build_id)
        if action not in {"start", "status", "stop", "pause", "resume"}:
            raise Rejected("Unknown dependency-build operation.")
        return self.request(f"/v1/dependency-builds/{build_id}/{action}", {})

    def pause_dependencies(self, build_id: str) -> dict:
        return self.dependencies(build_id, "pause")

    def resume_dependencies(self, build_id: str) -> dict:
        return self.dependencies(build_id, "resume")

    def purge_dependencies(
        self, build_id: str, *, run_id: str, resources=("source", "logs")
    ) -> dict:
        identifier(build_id)
        identifier(run_id)
        return self.request(
            f"/v1/dependency-builds/{build_id}/purge",
            {"run_id": run_id, "resources": list(resources)},
        )

    def upload(self, kind: str, content: bytes) -> dict:
        return self.request(
            "/v1/artifacts",
            {
                "kind": kind,
                "sha256": digest(content),
                "content": base64.b64encode(content).decode(),
            },
        )

    def prepare(self, manifest: dict) -> dict:
        return self.request("/v1/prepare", manifest)

    def operation(
        self, operation_id: str, action: str, data: dict | None = None
    ) -> dict:
        identifier(operation_id)
        if action not in {
            "start",
            "status",
            "stop",
            "pause",
            "resume",
            "purge",
            "logs",
            "secret",
            "result",
        }:
            raise Rejected("Unknown executor operation.")
        return self.request(f"/v1/operations/{operation_id}/{action}", data or {})

    def start(self, operation_id: str) -> dict:
        return self.operation(operation_id, "start")

    def status(self, operation_id: str) -> dict:
        return self.operation(operation_id, "status")

    def stop(self, operation_id: str) -> dict:
        return self.operation(operation_id, "stop")

    def pause(self, operation_id: str) -> dict:
        return self.operation(operation_id, "pause")

    def resume(self, operation_id: str) -> dict:
        return self.operation(operation_id, "resume")

    def purge(
        self, operation_id: str, *, run_id: str, resources=("source", "logs")
    ) -> dict:
        identifier(run_id)
        return self.operation(
            operation_id, "purge", {"run_id": run_id, "resources": list(resources)}
        )

    def supply_secret(self, operation_id: str, secret: str) -> dict:
        return self.operation(operation_id, "secret", {"secret": secret})

    def logs(self, operation_id: str, offset: int = 0, limit: int = 65536) -> bytes:
        result = self.operation(
            operation_id, "logs", {"offset": offset, "limit": limit}
        )
        content = base64.b64decode(result["content"], validate=True)
        if len(content) > limit or result["next_offset"] != offset + len(content):
            raise ExecutorUnavailable("Invalid log byte range.")
        return content

    def export(self, operation_id: str, source: bytes) -> tuple[dict, bytes]:
        result = self.operation(operation_id, "result")
        content = base64.b64decode(result["content"], validate=True)
        if canonical(result_manifest(source, content)) != canonical(result["manifest"]):
            raise ExecutorUnavailable(
                "Executor returned an invalid result digest/inventory."
            )
        return result["manifest"], content
