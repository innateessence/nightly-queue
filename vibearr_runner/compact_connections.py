"""Selected connections and revocable egress grants owned by the trusted runner."""

import fcntl
import argparse
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
import math
import os
from pathlib import Path
import secrets
import signal
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from vibearr_executor.contracts import (
    Conflict,
    Rejected,
    canonical,
    digest,
    identifier,
    sha256,
)
from vibearr_executor.managed_login import FILES, ROOT, bounded_json, validate
from vibearr_executor.ollama_relay import external_endpoint
from vibearr_executor.runtime import durable


SCOPE_FIELDS = {
    "operation_id",
    "phase",
    "worker_instance",
    "worker_boot",
    "binding_digest",
    "source_digest",
    "context_digest",
}
HOSTS = {
    "codex": {"chatgpt.com", "auth.openai.com"},
    "claude": {"api.anthropic.com", "claude.ai", "platform.claude.com"},
    "setup": {"pypi.org", "files.pythonhosted.org"},
}
MAX_BODY = 32768


def checked_scope(value):
    if not isinstance(value, dict) or set(value) != SCOPE_FIELDS:
        raise Rejected("Connection scope is incomplete.")
    for key in ("operation_id", "worker_instance", "worker_boot"):
        identifier(value[key])
    for key in ("binding_digest", "source_digest", "context_digest"):
        sha256(value[key])
    if value["phase"] not in {"agent", "setup", "validation"}:
        raise Rejected("Unknown connection phase.")
    return value


def bounded_login(provider, files):
    if not isinstance(files, dict) or provider not in FILES:
        raise Rejected("Select a native subscription login.")
    if set(files) - set(FILES[provider]) or len(canonical(files)) > 12000:
        raise Rejected("Subscription login files differ from the registered profile.")
    if any(not isinstance(value, dict) for value in files.values()):
        raise Rejected("Subscription login files must be objects.")
    if provider == "claude" and ".claude.json" in files:
        settings = files[".claude.json"]
        if set(settings) - {"hasCompletedOnboarding", "oauthAccount"}:
            raise Rejected("Claude login cannot contain task settings.")
        identity = settings.get("oauthAccount", {})
        if not isinstance(identity, dict) or set(identity) - {
            "accountUuid",
            "organizationUuid",
        }:
            raise Rejected("Claude selected account identity is invalid.")
        if any(
            not isinstance(value, str) or not value or len(value) > 200
            for value in identity.values()
        ):
            raise Rejected("Claude selected account identity is invalid.")
    try:
        json.dumps(files, allow_nan=False)
        validate(provider, files)
    except (KeyError, ValueError, TypeError):
        raise Rejected("Subscription login is incomplete.") from None
    return files


class CompactConnections:
    def __init__(self, directory, worker, *, login_root=ROOT, now=time.time):
        self.directory = Path(directory)
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.directory.chmod(0o700)
        self.manager_lock = (self.directory / "manager.lock").open("a")
        try:
            fcntl.flock(self.manager_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.manager_lock.close()
            raise Conflict(
                "Another connection controller owns this runner state."
            ) from None
        self.login_root = Path(login_root)
        self.login_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.login_root.chmod(0o700)
        self.worker, self.now = worker, now
        self.lock = threading.RLock()
        self.login_lock = None
        self.login_grants = {}
        self._restore_lock()

    def _restore_lock(self):
        if any(
            self._read(path.stem)["state"] != "finished"
            for path in self.directory.glob("*.json")
        ):
            self._lock_login()

    def _lock_login(self):
        if self.login_lock is not None:
            return
        if any(
            self.now() < grant["expires_at"] for grant in self.login_grants.values()
        ):
            raise Conflict(
                "A managed login operation still owns authentication egress."
            )
        stream = (self.login_root / "execution.lock").open("a")
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            stream.close()
            raise Conflict("Another native login operation owns the runner.") from None
        self.login_lock = stream

    def _unlock_if_idle(self):
        if any(
            self._read(path.stem)["state"] != "finished"
            for path in self.directory.glob("*.json")
        ):
            return
        if self.login_lock is not None:
            self.login_lock.close()
            self.login_lock = None
        (self.login_root / "execution-lease.json").unlink(missing_ok=True)

    def _path(self, operation_id):
        return self.directory / (identifier(operation_id) + ".json")

    def _read(self, operation_id):
        return bounded_json(self._path(operation_id))

    def _save(self, record):
        durable(self._path(record["scope"]["operation_id"]), canonical(record))
        if record["state"] != "finished":
            durable(
                self.login_root / "execution-lease.json", canonical(record["scope"])
            )

    def _login_path(self, provider, credential_id):
        if provider not in FILES or type(credential_id) is not int or credential_id < 1:
            raise Rejected("Choose a selected provider and positive credential ID.")
        path = self.login_root / provider / str(credential_id)
        if any(parent.is_symlink() for parent in (self.login_root, path.parent, path)):
            raise Rejected("Login storage cannot contain links.")
        return path / "login.json"

    def _login(self, provider, credential_id):
        return bounded_login(
            provider, bounded_json(self._login_path(provider, credential_id))
        )

    def status(self, provider, credential_id):
        with self.lock:
            try:
                files = self._login(provider, credential_id)
            except (OSError, ValueError, KeyError):
                return {"available": False}
            return {"available": True, "login_digest": digest(canonical(files))}

    def _current_scope(self, scope):
        checked_scope(scope)
        capabilities = self.worker.capabilities()
        expected = {
            "worker_instance": capabilities.get("instance_id"),
            "worker_boot": capabilities.get("worker_boot"),
            "binding_digest": capabilities.get("binding_digest"),
        }
        if any(scope[key] != value for key, value in expected.items()):
            raise Conflict("Connection scope belongs to another worker boot.")
        bound = self.worker.request(
            f"/v4/operations/{scope['operation_id']}/connection-binding"
        )
        if bound.get("scope") != scope:
            raise Conflict(
                "Connection scope differs from the registered worker operation."
            )
        return bound

    def prepare(self, scope, connection, *, model, deadline):
        with self.lock:
            bound = self._current_scope(scope)
            if scope["phase"] != "agent" or not isinstance(connection, dict):
                raise Rejected("Only an agent receives a selected native connection.")
            if type(deadline) not in {int, float} or not math.isfinite(deadline):
                raise Rejected("Connection deadline is invalid.")
            if not self.now() < deadline <= self.now() + 3600:
                raise Rejected("Connection deadline is outside its bound.")
            if not isinstance(model, str) or not model or len(model) > 200:
                raise Rejected("Select the bound native model.")
            provider = connection.get("provider")
            if bound.get("provider") != provider or bound.get("model") != model:
                raise Rejected(
                    "Selected provider or model differs from the registered native operation."
                )
            if type(bound.get("credential_id")) is not int or bound[
                "credential_id"
            ] != connection.get("credential_id"):
                raise Rejected(
                    "Selected account differs from the registered native operation."
                )
            allowed = {"provider", "credential_id", "login_digest"}
            if provider == "opencode":
                allowed = {"provider", "credential_id", "endpoint"}
            if (
                set(connection) != allowed
                or type(connection.get("credential_id")) is not int
                or connection["credential_id"] < 1
            ):
                raise Rejected("Selected native connection is invalid.")
            selected = dict(connection)
            files = None
            if provider == "opencode":
                selected["endpoint"] = external_endpoint(selected["endpoint"])
            elif provider in FILES:
                files = self._login(provider, selected["credential_id"])
                if selected["login_digest"] != digest(canonical(files)):
                    raise Conflict("Selected login changed; repeat native readiness.")
            else:
                raise Rejected("Unsupported selected native provider.")
            intent = {
                "scope": scope,
                "connection": selected,
                "model": model,
                "deadline": deadline,
            }
            path = self._path(scope["operation_id"])
            if path.exists():
                record = self._read(scope["operation_id"])
                if record["intent"] != intent or record["state"] in {
                    "finished",
                    "revoked",
                }:
                    raise Conflict(
                        "Connection operation already binds another intent or is terminal."
                    )
            else:
                self._lock_login()
                records = list(self.directory.glob("*.json"))
                if len(records) >= 1000:
                    raise Conflict(
                        "Connection journal capacity requires retention before new work."
                    )
                if any(
                    self._read(item.stem)["state"] != "finished" for item in records
                ):
                    raise Conflict("Another connection operation needs reconciliation.")
                record = {
                    "scope": scope,
                    "intent": intent,
                    "token": secrets.token_urlsafe(48),
                    "expires_at": deadline,
                    "state": "prepared",
                }
                self._save(record)
            if files is not None:
                selected["files"] = files
            envelope = {
                "schema_version": 1,
                "scope": dict(scope),
                "connection": selected,
                "grant": {"token": record["token"], "expires_at": record["expires_at"]},
            }
            if len(canonical(envelope)) > 16384:
                raise Rejected(
                    "Selected operation connection exceeds the worker credential bound."
                )
            return envelope

    def _bound(self, scope, token):
        self._current_scope(scope)
        record = self._read(scope["operation_id"])
        if (
            record["scope"] != scope
            or not isinstance(token, str)
            or not hmac.compare_digest(record["token"], token)
        ):
            raise Rejected("Connection grant differs from the bound operation.")
        return record

    def activate(self, scope, token="", *, model=""):
        with self.lock:
            bound = self._current_scope(scope)
            if scope["phase"] == "validation":
                raise Rejected("Validation has no public egress grant.")
            if scope["phase"] == "setup":
                path = self._path(scope["operation_id"])
                if not path.exists():
                    records = list(self.directory.glob("*.json"))
                    if len(records) >= 1000:
                        raise Conflict(
                            "Connection journal capacity requires retention before new work."
                        )
                    if any(
                        self._read(item.stem)["state"] != "finished" for item in records
                    ):
                        raise Conflict("Another phase owns network mediation.")
                    self._lock_login()
                    deadline = datetime.fromisoformat(bound["deadline"]).timestamp()
                    self._save(
                        {
                            "scope": scope,
                            "intent": {
                                "connection": {"provider": "setup"},
                                "model": "",
                            },
                            "token": secrets.token_urlsafe(48),
                            "expires_at": min(deadline, self.now() + 1800),
                            "state": "prepared",
                        }
                    )
                record = self._read(scope["operation_id"])
                if record["scope"] != scope:
                    raise Conflict("Setup scope changed during replay.")
            else:
                record = self._bound(scope, token)
                if model != record["intent"]["model"]:
                    raise Rejected("Model differs from the selected connection.")
            if (
                record["state"] in {"finished", "revoked"}
                or self.now() >= record["expires_at"]
            ):
                raise Conflict("Connection grant expired or is terminal.")
            record["state"] = "active"
            self._save(record)
            return {
                "token": record["token"],
                "expires_at": record["expires_at"],
                "provider": record["intent"]["connection"]["provider"],
            }

    def suspend(self, scope, token):
        with self.lock:
            record = self._bound(scope, token)
            if record["state"] not in {"active", "suspended"}:
                raise Conflict(
                    "Only an active operation can suspend its network grant."
                )
            record["state"] = "suspended"
            self._save(record)
            return {"suspended": True}

    def revoke(self, scope, token):
        with self.lock:
            record = self._bound(scope, token)
            if record["state"] != "finished":
                record["state"] = "revoked"
                self._save(record)
            return {"revoked": True}

    def grant(self, token, *, host=None, model=None):
        with self.lock:
            if not isinstance(token, str) or not token:
                raise Rejected("Operation egress grant is missing.")
            login = self.login_grants.get(token)
            if login is not None:
                if (
                    self.now() >= login["expires_at"]
                    or self.login_lock is not None
                    or not self._login_lease_held()
                    or model is not None
                    or host not in HOSTS[login["provider"]]
                ):
                    raise Rejected(
                        "Login grant is expired or outside provider authentication."
                    )
                return {"provider": login["provider"]}
            matching = [self._read(path.stem) for path in self.directory.glob("*.json")]
            matching = [
                record
                for record in matching
                if hmac.compare_digest(record["token"], token)
            ]
            if len(matching) != 1:
                raise Rejected("Operation egress grant is unknown.")
            record = matching[0]
            if record["state"] != "active" or self.now() >= record["expires_at"]:
                raise Rejected("Operation egress grant is inactive or expired.")
            bound = self._current_scope(record["scope"])
            if bound.get("state") != "running":
                raise Rejected("Operation egress requires the active worker task.")
            selected = record["intent"]["connection"]
            provider = selected["provider"]
            if host is not None and host not in HOSTS.get(provider, set()):
                raise Rejected("Destination is outside the active phase.")
            if model is not None and (
                provider != "opencode" or model != record["intent"]["model"]
            ):
                raise Rejected("Inference differs from the selected external model.")
            return selected

    def _login_lease_held(self):
        with (self.login_root / "execution.lock").open("a") as stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
        return False

    def relay(self, token, model=None):
        with self.lock:
            selected = self.grant(token)
            if selected["provider"] != "opencode":
                raise Rejected(
                    "Only the selected external Ollama connection permits relay access."
                )
            records = [self._read(path.stem) for path in self.directory.glob("*.json")]
            record = next(
                record
                for record in records
                if hmac.compare_digest(record["token"], token)
            )
            if model is not None and model != record["intent"]["model"]:
                raise Rejected("Inference differs from the selected external model.")
            return {
                "endpoint": selected["endpoint"],
                "model": record["intent"]["model"],
            }

    def begin_login(self, provider):
        with self.lock:
            self.login_grants = {
                token: grant
                for token, grant in self.login_grants.items()
                if self.now() < grant["expires_at"]
            }
            if (
                provider not in FILES
                or self.login_lock is not None
                or self.login_grants
            ):
                raise Conflict("Another coding or login operation owns the runner.")
            if not self._login_lease_held():
                raise Conflict("A managed login process must hold the login lease.")
            token = secrets.token_urlsafe(48)
            self.login_grants[token] = {
                "provider": provider,
                "expires_at": self.now() + 900,
            }
            return {
                "token": token,
                "expires_at": self.login_grants[token]["expires_at"],
            }

    def end_login(self, token):
        with self.lock:
            self.login_grants.pop(token, None)
            return {"revoked": True}

    def finish(self, scope, token, *, files=None, terminated=False):
        with self.lock:
            record = self._bound(scope, token)
            if terminated is not True:
                raise Conflict(
                    "Confirm task termination before returning a connection."
                )
            provider = record["intent"]["connection"]["provider"]
            new_digest = digest(canonical(files)) if files is not None else ""
            if (
                record.get("returned_digest")
                and record["returned_digest"] != new_digest
            ):
                raise Conflict("Connection renewal replay changed.")
            if record["state"] == "finished":
                if record.get("returned_digest", "") != new_digest:
                    raise Conflict("Connection completion replay changed.")
                return {"finished": True, "login_digest": new_digest}
            if provider in FILES and files is None:
                raise Conflict(
                    "Return the selected subscription login before releasing its fence."
                )
            if files is not None:
                bounded_login(provider, files)
                selected = record["intent"]["connection"]
                current = self._login(provider, selected["credential_id"])
                if (
                    provider == "codex"
                    and current[".codex/auth.json"]["tokens"]["account_id"]
                    != files[".codex/auth.json"]["tokens"]["account_id"]
                ):
                    raise Rejected("Refreshed login belongs to another account.")
                if provider == "claude" and files != current:
                    old = current.get(".claude.json", {}).get("oauthAccount", {})
                    new = files.get(".claude.json", {}).get("oauthAccount", {})
                    identity = {
                        key: old[key]
                        for key in ("accountUuid", "organizationUuid")
                        if old.get(key)
                    }
                    if not identity or any(
                        new.get(key) != value for key, value in identity.items()
                    ):
                        raise Rejected(
                            "Refreshed Claude login lacks the selected account identity; reconcile the original login."
                        )
                current_digest = digest(canonical(current))
                if current_digest not in {selected["login_digest"], new_digest}:
                    raise Conflict("Selected login changed during the operation.")
                record["state"] = "returning"
                record["returned_digest"] = new_digest
                self._save(record)
                path = self._login_path(provider, selected["credential_id"])
                durable(path, canonical(files))
            record.update(state="finished", returned_digest=new_digest)
            self._save(record)
            self._unlock_if_idle()
            return {"finished": True, "login_digest": new_digest}

    def close(self):
        self.login_grants.clear()
        if self.login_lock is not None:
            self.login_lock.close()
            self.login_lock = None
        self.manager_lock.close()

    def reconcile(self):
        with self.lock:
            for path in self.directory.glob("*.json"):
                record = self._read(path.stem)
                if record["state"] == "finished":
                    continue
                observation = self._current_scope(record["scope"])
                if (
                    observation.get("state") != "exited"
                    or observation.get("descendants_terminated") is not True
                ):
                    continue
                provider = record["intent"]["connection"]["provider"]
                if provider in FILES:
                    selected = record["intent"]["connection"]
                    returned = record.get("returned_digest")
                    persisted = digest(
                        canonical(self._login(provider, selected["credential_id"]))
                    )
                    if not returned or persisted != returned:
                        record.update(state="revoked", renewal_unverified=True)
                        self._save(record)
                        continue
                record.update(
                    state="finished", returned_digest=record.get("returned_digest", "")
                )
                self._save(record)
            self._unlock_if_idle()


class ConnectionServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, connections, controller_token, worker_token):
        if (
            len(controller_token) < 32
            or len(worker_token) < 32
            or hmac.compare_digest(controller_token, worker_token)
        ):
            raise ValueError(
                "Use separate private controller and worker mediation credentials."
            )
        self.connections = connections
        self.controller_token, self.worker_token = controller_token, worker_token
        super().__init__(address, ConnectionHandler)


class ConnectionHandler(BaseHTTPRequestHandler):
    server: ConnectionServer

    def log_message(self, *_args):
        pass

    def do_GET(self):
        expected = "Bearer " + self.server.controller_token
        if self.path != "/health" or not hmac.compare_digest(
            self.headers.get("Authorization", ""), expected
        ):
            self.send_error(403)
            return
        content = b'{"healthy":true}'
        self.send_response(200)
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def do_POST(self):
        try:
            self.connection.settimeout(10)
            length = int(self.headers.get("Content-Length", "0"))
            if self.headers.get("Transfer-Encoding") or not 0 < length <= MAX_BODY:
                raise Rejected("Invalid mediation request size.")
            controller_paths = {
                "/v4/connections/status",
                "/v4/connections/prepare",
                "/v4/logins/begin",
                "/v4/logins/end",
            }
            token = (
                self.server.controller_token
                if self.path in controller_paths
                else self.server.worker_token
            )
            if not hmac.compare_digest(
                self.headers.get("Authorization", ""), "Bearer " + token
            ):
                raise Rejected("Mediation authentication failed.")
            body = self.rfile.read(length)
            if len(body) != length:
                raise Rejected("Incomplete mediation request.")
            data = json.loads(body)
            if not isinstance(data, dict):
                raise Rejected("Invalid mediation request.")
            manager = self.server.connections
            if self.path == "/v4/logins/begin" and set(data) == {"provider"}:
                result = manager.begin_login(data["provider"])
            elif self.path == "/v4/logins/end" and set(data) == {"token"}:
                result = manager.end_login(data["token"])
            elif self.path == "/v4/connections/status" and set(data) == {
                "provider",
                "credential_id",
            }:
                result = manager.status(data["provider"], data["credential_id"])
            elif self.path == "/v4/connections/prepare" and set(data) == {
                "scope",
                "connection",
                "model",
                "deadline",
            }:
                result = manager.prepare(
                    data["scope"],
                    data["connection"],
                    model=data["model"],
                    deadline=data["deadline"],
                )
            elif self.path == "/v4/connections/activate" and set(data) <= {
                "scope",
                "token",
                "model",
            }:
                result = manager.activate(
                    data["scope"], data.get("token", ""), model=data.get("model", "")
                )
            elif self.path == "/v4/connections/suspend" and set(data) == {
                "scope",
                "token",
            }:
                result = manager.suspend(data["scope"], data["token"])
            elif self.path == "/v4/connections/revoke" and set(data) == {
                "scope",
                "token",
            }:
                result = manager.revoke(data["scope"], data["token"])
            elif self.path == "/v4/connections/finish" and set(data) <= {
                "scope",
                "token",
                "files",
                "terminated",
            }:
                result = manager.finish(
                    data["scope"],
                    data["token"],
                    files=data.get("files"),
                    terminated=data.get("terminated", False),
                )
            else:
                raise Rejected("Unsupported mediation operation.")
            content = canonical(result)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
        except (OSError, ValueError, KeyError, TypeError):
            self.send_error(
                409, "Connection operation rejected; reconcile the original operation."
            )


class MediationClient:
    def __init__(self, origin, token, *, timeout=30):
        parsed = urllib.parse.urlsplit(origin)
        if (
            parsed.scheme != "http"
            or parsed.hostname not in {"runner", "127.0.0.1", "localhost"}
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise Rejected("Choose the registered private runner mediation origin.")
        if len(token) < 32:
            raise Rejected("Private mediation authentication is missing.")
        self.origin, self.token, self.timeout = origin.rstrip("/"), token, timeout
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), NoRedirect()
        )

    def request(self, path, data=None):
        if not path.startswith(("/v4/connections/", "/v4/logins/", "/health")):
            raise Rejected("Unknown mediation route.")
        request = urllib.request.Request(
            self.origin + path,
            data=canonical(data) if data is not None else None,
            headers={
                "Authorization": "Bearer " + self.token,
                "Content-Type": "application/json",
            },
        )
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                content = response.read(MAX_BODY + 1)
                if len(content) > MAX_BODY:
                    raise Rejected("Mediation response exceeds its bound.")
                result = json.loads(content)
                if not isinstance(result, dict):
                    raise Rejected("Mediation returned an invalid response.")
                return result
        except urllib.error.HTTPError as exc:
            exc.close()
            raise Conflict(
                "Mediation outcome is uncertain; reconcile the original operation."
            ) from None
        except (OSError, ValueError, urllib.error.URLError):
            raise Conflict(
                "Mediation outcome is uncertain; reconcile the original operation."
            ) from None


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, _request, _file, _code, _message, _headers, _url):
        return None


class CompactClient:
    def __init__(self, worker, origin, token, *, directory=None):
        self.worker = worker
        self.mediation = MediationClient(origin, token)
        root = (
            directory
            or Path(os.environ["VIBEARR_RUNNER_STATE_DIR"]) / "connection-inputs"
        )
        self.directory = Path(root)
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.directory.chmod(0o700)
        self.compact = True
        self.integrated = False

    def __getattr__(self, name):
        return getattr(self.worker, name)

    def request(self, path, data=None):
        if path.startswith("/v3/logins/") and data is None:
            parts = path.split("/")
            if len(parts) != 5:
                raise Rejected("Select a subscription login.")
            return self.mediation.request(
                "/v4/connections/status",
                {"provider": parts[3], "credential_id": int(parts[4])},
            )
        return self.worker.request(path, data)

    def prepare(self, manifest):
        operation_id = identifier(manifest.get("operation_id"))
        path = self.directory / (operation_id + ".json")
        if path.exists() and bounded_json(path) != manifest:
            raise Conflict("Native connection manifest changed during replay.")
        durable(path, canonical(manifest))
        return self.worker.prepare(manifest)

    def supply_secret(self, operation_id, secret):
        manifest = bounded_json(self.directory / (identifier(operation_id) + ".json"))
        binding = self.worker.request(
            f"/v4/operations/{operation_id}/connection-binding"
        )
        scope = checked_scope(binding["scope"])
        if any(
            scope[key] != manifest[key] for key in ("source_digest", "context_digest")
        ):
            raise Conflict(
                "Worker connection input differs from the prepared native task."
            )
        envelope = self.mediation.request(
            "/v4/connections/prepare",
            {
                "scope": scope,
                "connection": json.loads(secret),
                "model": binding["model"],
                "deadline": datetime.fromisoformat(manifest["deadline"]).timestamp(),
            },
        )
        return self.worker.supply_secret(operation_id, canonical(envelope).decode())


def _controller_identity():
    os.setgroups([])
    os.setgid(10001)
    os.setuid(10001)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "action", choices=["serve", "health"], default="serve", nargs="?"
    )
    args = parser.parse_args()
    if os.environ.get("VIBEARR_RUNTIME_PROTOCOL") != "compact-worker-v1":
        raise Rejected(
            "Runner connection mediation requires the dedicated compact worker."
        )
    state = Path(os.environ.get("VIBEARR_RUNNER_STATE_DIR", "/var/lib/vibearr"))
    token_path = state / "mediation-controller-token"
    if args.action == "health":
        result = MediationClient(
            "http://127.0.0.1:8792", token_path.read_text().strip()
        ).request("/health")
        if result.get("healthy") is not True:
            raise SystemExit("Runner mediation is unhealthy.")
        return
    from contextlib import ExitStack
    from vibearr_executor.client import Client
    from vibearr_executor.proxy import Handler as ProxyHandler, Proxy
    from vibearr_executor.ollama_relay import Handler as RelayHandler, Relay

    os.umask(0o077)
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chown(state, 10001, 10001)
    if not token_path.exists():
        durable(token_path, secrets.token_urlsafe(48).encode())
    os.chown(token_path, 10001, 10001)
    controller_token = token_path.read_text().strip()
    from vibearr_executor.control_token import read_control_token

    worker_token = read_control_token(os.environ["VIBEARR_MEDIATION_TOKEN_FILE"])
    worker = Client(
        os.environ["VIBEARR_EXECUTOR_URL"],
        read_control_token(os.environ["VIBEARR_EXECUTOR_TOKEN_FILE"]),
    )
    manager = CompactConnections(state / "connections", worker)
    capabilities = worker.capabilities()
    from vibearr_executor.validation_profile import ValidationProfile

    profile = ValidationProfile.parse(capabilities.get("validation_profile"))
    if capabilities.get(
        "runtime"
    ) != "compact-worker-v1" or profile.image != os.environ.get(
        "VIBEARR_WORKER_IMAGE_ID"
    ):
        raise Rejected(
            "Registered validation tools differ from the selected compact worker image."
        )
    profile_path = state / "validation-profile.json"
    durable(profile_path, canonical(profile.data()))
    os.chown(profile_path, 10001, 10001)
    stop = threading.Event()
    with ExitStack() as stack:
        server = stack.enter_context(
            ConnectionServer(("0.0.0.0", 8792), manager, controller_token, worker_token)
        )
        proxy = stack.enter_context(Proxy(("0.0.0.0", 3128), ProxyHandler))
        denied = set()
        for host in ("app", "postgres", "runner", "worker"):
            try:
                denied.update(
                    item[4][0]
                    for item in socket.getaddrinfo(host, None, socket.AF_INET)
                )
            except OSError:
                pass
        proxy.hosts, proxy.denied = set().union(*HOSTS.values()), denied
        proxy.authorize = lambda token, host: manager.grant(token, host=host)
        relay = stack.enter_context(Relay(("0.0.0.0", 3130), RelayHandler))
        relay.select_connection = manager.relay
        relay.denied = denied
        for service in (server, proxy, relay):
            threading.Thread(target=service.serve_forever, daemon=True).start()
        names = {
            "PATH",
            "VIBEARR_URL",
            "VIBEARR_RUNNER_STATE_DIR",
            "VIBEARR_RUNNER_API_TOKEN_FILE",
            "VIBEARR_EXECUTOR_URL",
            "VIBEARR_EXECUTOR_TOKEN_FILE",
            "VIBEARR_RUNTIME_PROTOCOL",
        }
        environment = {key: value for key, value in os.environ.items() if key in names}
        environment.update(
            HOME=str(state),
            VIBEARR_MEDIATION_URL="http://127.0.0.1:8792",
            VIBEARR_MEDIATION_CONTROLLER_TOKEN_FILE=str(token_path),
            VIBEARR_VALIDATION_PROFILE_FILE=str(profile_path),
        )
        controller = subprocess.Popen(
            ["python", "-I", "-m", "vibearr_runner"],
            cwd="/opt/executor",
            env=environment,
            preexec_fn=_controller_identity,
            start_new_session=True,
        )
        for signum in (signal.SIGTERM, signal.SIGINT):
            signal.signal(signum, lambda *_: stop.set())
        try:
            next_reconcile = 0.0
            while not stop.wait(0.5):
                if controller.poll() is not None:
                    raise RuntimeError("Controller exited; restart the runner.")
                if time.monotonic() >= next_reconcile:
                    from vibearr_executor.client import ExecutorUnavailable

                    try:
                        manager.reconcile()
                    except ExecutorUnavailable:
                        pass
                    next_reconcile = time.monotonic() + 5
        finally:
            if controller.poll() is None:
                os.killpg(controller.pid, signal.SIGTERM)
                try:
                    controller.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    os.killpg(controller.pid, signal.SIGKILL)
                    controller.wait()
            for service in (server, proxy, relay):
                service.shutdown()
            manager.close()


if __name__ == "__main__":
    main()
