import json
import math
from http.client import HTTPException
import socket
import threading
import time
from typing import Any, Callable, NoReturn
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


class APIError(Exception):
    pass


class AuthenticationError(APIError):
    pass


class ConflictError(APIError):
    pass


class NotFoundError(APIError):
    pass


class InvalidResponse(APIError):
    pass


class UnavailableError(APIError):
    pass


class AmbiguousRequest(APIError):
    """Reconcile before repeating a mutation, including after an invalid response."""


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Client:
    def __init__(
        self, url: str, token: str, timeout: float = 15, max_bytes: int = 1048576
    ):
        parsed = urlsplit(url)
        invalid = parsed.username or parsed.password or parsed.query or parsed.fragment
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or invalid:
            raise ValueError(
                "Use an HTTP(S) queue URL without credentials, query, or fragment."
            )
        if not token or any(character.isspace() for character in token):
            raise ValueError("A runner bearer token is required.")
        if not 0 < timeout <= 60 or not 0 < max_bytes <= 8388608:
            raise ValueError("Request limits are out of range.")
        self.url = url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self.max_bytes = max_bytes
        self.controller_secret: str | None = None
        self.opener = build_opener(ProxyHandler({}), NoRedirects())
        self.request_lock = threading.Lock()

    def request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
        checkpoint: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        if not path.startswith("/api/v1/runner/") or "?" in path or "#" in path:
            raise ValueError("Use a runner API path.")
        body = (
            None if payload is None else json.dumps(payload, allow_nan=False).encode()
        )
        if body is not None and len(body) > self.max_bytes:
            raise ValueError("Request exceeds size limit.")
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
        }
        if self.controller_secret is not None:
            headers["X-Vibearr-Controller-Secret"] = self.controller_secret
            headers["X-NQ-Controller-Secret"] = self.controller_secret
        if body is not None:
            headers["Content-Type"] = "application/json"
        request = Request(self.url + path, data=body, headers=headers, method=method)
        mutation = method != "GET"
        if timeout is not None and (not math.isfinite(timeout) or timeout <= 0):
            raise ValueError("Request timeout must be finite and positive.")
        if checkpoint is not None and checkpoint() is not True:
            raise UnavailableError("Request interrupted before transport.")
        allowance = min(self.timeout, timeout) if timeout is not None else self.timeout
        deadline = time.monotonic() + allowance
        if not self.request_lock.acquire(blocking=False):
            self._unavailable(mutation)
        finished = threading.Event()
        outcome: list[Any] = []

        def send():
            try:
                outcome.append(self._send(request, mutation, deadline))
            except Exception as exc:
                outcome.append(exc)
            finally:
                self.request_lock.release()
                finished.set()

        # Socket timeouts alone do not bound DNS or a peer dripping HTTP headers.
        worker = threading.Thread(target=send, daemon=True)
        worker.start()
        while True:
            if checkpoint is not None and checkpoint() is not True:
                self._unavailable(mutation)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._unavailable(mutation)
            if finished.wait(min(0.1, remaining)):
                break
        if time.monotonic() >= deadline:
            self._unavailable(mutation)
        if checkpoint is not None and checkpoint() is not True:
            self._unavailable(mutation)
        result = outcome[0]
        if isinstance(result, Exception):
            raise result
        return result

    def _unavailable(self, mutation: bool) -> NoReturn:
        error = AmbiguousRequest if mutation else UnavailableError
        raise error(
            "Queue unavailable; mutation outcomes require reconciliation."
        ) from None

    def _send(
        self, request: Request, mutation: bool, deadline: float
    ) -> dict[str, Any]:
        try:
            remaining = min(self.timeout, deadline - time.monotonic())
            if remaining <= 0:
                self._unavailable(mutation)
            with self.opener.open(request, timeout=remaining) as response:
                if response.status != 200:
                    raise InvalidResponse("Unexpected successful response status.")
                if response.headers.get_content_type() != "application/json":
                    raise InvalidResponse("Expected a JSON response.")
                chunks = bytearray()
                while True:
                    remaining = self.max_bytes + 1 - len(chunks)
                    chunk = response.read1(min(65536, remaining))
                    chunks.extend(chunk)
                    if len(chunks) > self.max_bytes or time.monotonic() > deadline:
                        raise InvalidResponse("Response exceeded request limits.")
                    if not chunk:
                        break
                data = json.loads(chunks)
                if not isinstance(data, dict):
                    raise InvalidResponse("Expected a JSON object.")
                return data
        except HTTPError as exc:
            status = exc.code
            exc.close()
            if status == 401 or status == 403:
                raise AuthenticationError("Runner authentication rejected.") from None
            if status == 409:
                raise ConflictError(
                    "Runner operation conflicted; reconcile before retrying."
                ) from None
            if status == 404:
                raise NotFoundError("Runner resource was not found.") from None
            if mutation and status >= 500:
                raise AmbiguousRequest(
                    "Mutation outcome unknown; reconcile before retrying."
                ) from None
            raise APIError(f"Runner API returned HTTP {status}.") from None
        except (URLError, TimeoutError, socket.timeout, OSError, HTTPException):
            self._unavailable(mutation)
        except (ValueError, InvalidResponse, RecursionError):
            response_error = AmbiguousRequest if mutation else InvalidResponse
            raise response_error(
                "Invalid queue response; mutation outcomes require reconciliation."
            ) from None

    def identity(self) -> dict[str, Any]:
        return self.request("GET", "/api/v1/runner/identity/")

    def configuration(self) -> dict[str, Any]:
        return self.request("GET", "/api/v1/runner/configuration/")

    def active(self) -> dict[str, Any]:
        return self.request("GET", "/api/v1/runner/runs/active/")

    def reconcile(self, run_id: str) -> dict[str, Any]:
        from uuid import UUID

        run_id = str(UUID(run_id))
        return self.request("GET", f"/api/v1/runner/runs/{run_id}/")

    def gate_report(self, report: dict[str, Any]) -> dict[str, Any]:
        return self.request("POST", "/api/v1/runner/gates/report/", {"report": report})
