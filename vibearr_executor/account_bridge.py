"""Only the current task's lease may renew through this loopback bridge."""

from datetime import datetime, timezone
import hmac
from http.server import BaseHTTPRequestHandler
import json
import threading
from uuid import uuid4

from .account_leases import lease_secret
from .codex_account import OAUTH_CLIENT, private_bytes
from .contracts import canonical, digest
from .runtime import durable
from .secret_material import protection_material
from .slot_http import SlotServer


BRIDGE_PORT = 8792


class AccountBridge:
    def __init__(self, runtime, client):
        self.runtime, self.client = runtime, client
        self.lock = threading.Lock()
        self.fenced = set()
        self.history = {}

    def bind(self, record, value):
        capsule = lease_secret(value)
        binding = capsule["binding"]
        manifest = record["manifest"]
        expected = {
            key: manifest[key]
            for key in (
                "operation_id",
                "run_id",
                "source_digest",
                "context_digest",
                "deadline",
            )
        }
        if (
            any(binding[key] != item for key, item in expected.items())
            or binding["execution_identity"] != record["execution_identity"]
        ):
            raise ValueError(
                "Account lease differs from the prepared worker operation."
            )
        context = json.loads(
            (self.runtime.artifacts / manifest["context_digest"]).read_bytes()
        )
        reference = binding["credential"]
        account = {
            "backend": "codex",
            "credential_id": reference["id"],
            "credential_version": reference["version"],
            "model": context.get("model"),
        }
        if (
            context.get("auth_kind") != "session"
            or context.get("account_digest") != binding["account_digest"]
            or digest(canonical(account)) != binding["account_digest"]
        ):
            raise ValueError("Account lease differs from the selected native account.")
        operation = manifest["operation_id"]
        durable(self.runtime.path(operation) / "account-lease.json", canonical(capsule))
        self.history.setdefault(operation, [])

    def active(self, record, capsule):
        operation = record["operation_id"]
        if (
            operation in self.fenced
            or self.runtime.active != operation
            or record["state"] not in {"launching", "running"}
            or record.get("paused")
        ):
            raise ValueError("Account renewal requires the current running task.")
        if (
            record["worker_boot"] != self.runtime.boot
            or capsule["binding"]["execution_identity"] != record["execution_identity"]
        ):
            raise ValueError("Account lease belongs to another worker boot.")
        deadline = datetime.fromisoformat(record["manifest"]["deadline"])
        if datetime.now(timezone.utc) >= deadline:
            raise ValueError("Account task deadline expired.")

    def renew(self, token):
        if not isinstance(token, str) or not 32 <= len(token) <= 128:
            raise ValueError("Account renewal capability is unavailable.")
        with self.lock:
            with self.runtime.lock:
                operation = self.runtime.active
                if not operation or operation in self.fenced:
                    raise ValueError(
                        "Account renewal requires the current running task."
                    )
                record = self.runtime.record(operation)
                capsule = lease_secret(
                    json.loads(
                        private_bytes(
                            self.runtime.path(operation) / "account-lease.json"
                        )
                    )
                )
                self.active(record, capsule)
                if not hmac.compare_digest(capsule["token"], token):
                    raise ValueError("Account renewal capability differs.")
                identity = record["execution_identity"]
            value = self.client.renew(operation, token, identity, str(uuid4()))
            auth = value["auth"]
            from .codex_task import auth_material

            auth_material("session", canonical(auth).decode(), runtime=True)
            if auth["tokens"]["account_id"] != capsule["auth"]["tokens"]["account_id"]:
                raise ValueError("Account renewal changed the selected account.")
            with self.runtime.lock:
                self.active(self.runtime.record(operation), capsule)
                history = self.history.setdefault(operation, [])
                if auth not in history:
                    if len(history) >= 16:
                        raise ValueError(
                            "Account renewal protection history exceeded its bound."
                        )
                    history.append(auth)
                    durable(
                        self.runtime.path(operation) / "account-protection.json",
                        canonical(history),
                    )
            tokens = auth["tokens"]
            return {
                "access_token": tokens["access_token"],
                "id_token": tokens["id_token"],
                "refresh_token": token,
                "token_type": "Bearer",
            }

    def protection(self, operation, secret, database_url):
        with self.runtime.lock:
            history = self.history.get(operation)
            if history is None:
                return protection_material(secret, database_url)
            return protection_material(
                canonical({"initial": json.loads(secret), "history": history}),
                database_url,
            )

    def fence(self, operation):
        with self.runtime.lock:
            self.fenced.add(operation)

    def close(self, operation):
        self.fence(operation)
        path = self.runtime.path(operation) / "account-lease.json"
        try:
            if not path.exists():
                return
            capsule = lease_secret(json.loads(private_bytes(path)))
            value = self.client.close(
                operation, capsule["token"], capsule["binding"]["execution_identity"]
            )
            if value.get("closed") is True:
                path.unlink()
        except (ValueError, OSError, KeyError, TypeError, RecursionError):
            pass

    def reconcile(self):
        for path in self.runtime.operations.iterdir():
            record = self.runtime.record(path.name)
            if record["state"] == "exited" and record.get("descendants_terminated"):
                self.close(path.name)
                self.release(path.name)

    def release(self, operation):
        with self.runtime.lock:
            self.history.pop(operation, None)
            self.fenced.discard(operation)


class BridgeServer(SlotServer):
    bridge: AccountBridge


class BridgeHandler(BaseHTTPRequestHandler):
    server: BridgeServer
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):
        pass

    def setup(self):
        super().setup()
        self.connection.settimeout(5)

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if (
                self.path != "/oauth/token"
                or not 0 < length <= 4096
                or self.headers.get("Transfer-Encoding")
            ):
                raise ValueError("Invalid OAuth bridge request.")
            content = self.rfile.read(length)
            if len(content) != length:
                raise ValueError("Truncated OAuth bridge request.")
            value = json.loads(content)
            if (
                not isinstance(value, dict)
                or value.get("grant_type") != "refresh_token"
                or value.get("client_id") != OAUTH_CLIENT
            ):
                raise ValueError("Select the fixed account refresh grant.")
            response = self.server.bridge.renew(value.get("refresh_token"))
            self.reply(200, response)
        except (ValueError, TypeError, KeyError, OSError):
            self.reply(
                400,
                {
                    "error": "invalid_grant",
                    "error_description": "The task account lease is unavailable or retired.",
                },
            )

    def do_GET(self):
        self.reply(405, {"error": "unsupported_grant_type"})

    def reply(self, status, value):
        content = canonical(value)
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Connection", "close")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(content)
