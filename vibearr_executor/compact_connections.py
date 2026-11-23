"""Operation-only worker access to trusted runner connection mediation."""

from datetime import datetime
import json
import math
import time
from urllib.parse import urlsplit

from vibearr_runner.compact_connections import (
    MediationClient,
    bounded_login,
    checked_scope,
)

from .contracts import Conflict, Rejected, canonical, digest
from .managed_login import collect
from .runtime import durable
from .secret_material import contains_secret
from .slot_network import address


class WorkerConnections:
    def __init__(self, origin, token, *, client=None, now=time.time):
        self.client = client or MediationClient(origin, token)
        self.origin, self.now = origin, now
        self.runtime = None

    def bind(self, runtime):
        self.runtime = runtime

    def _scope(self, operation_id):
        return checked_scope(self.runtime.connection_binding(operation_id)["scope"])

    def _path(self, operation_id):
        return self.runtime.path(operation_id) / "mediation.json"

    def _state(self, operation_id):
        return json.loads(self._path(operation_id).read_bytes())

    def validate_secret(self, operation_id, supplied_secret, record):
        if (
            not isinstance(supplied_secret, str)
            or len(supplied_secret.encode()) > 16384
        ):
            raise Rejected("Native connection envelope exceeds its bound.")
        envelope = json.loads(supplied_secret)
        if (
            not isinstance(envelope, dict)
            or set(envelope) != {"schema_version", "scope", "connection", "grant"}
            or type(envelope["schema_version"]) is not int
            or envelope["schema_version"] != 1
        ):
            raise Rejected("Unsupported native connection envelope.")
        scope = self._scope(operation_id)
        if envelope["scope"] != scope or scope["phase"] != "agent":
            raise Conflict(
                "Selected native connection differs from the worker operation."
            )
        context = json.loads(
            (self.runtime.artifacts / record["manifest"]["context_digest"]).read_bytes()
        )
        connection, grant = envelope["connection"], envelope["grant"]
        provider = context.get("adapter")
        if (
            not isinstance(connection, dict)
            or connection.get("provider") != provider
            or type(connection.get("credential_id")) is not int
            or connection["credential_id"] < 1
        ):
            raise Rejected("Selected connection differs from the native provider.")
        if connection["credential_id"] != context.get("credential_id"):
            raise Rejected("Selected connection differs from the native account.")
        if provider == "opencode":
            from .ollama_relay import external_endpoint

            if set(connection) != {"provider", "credential_id", "endpoint"}:
                raise Rejected("Unexpected external model connection fields.")
            external_endpoint(connection["endpoint"])
        else:
            if set(connection) != {
                "provider",
                "credential_id",
                "login_digest",
                "files",
            }:
                raise Rejected("Unexpected subscription connection fields.")
            bounded_login(provider, connection["files"])
            if connection["login_digest"] != digest(canonical(connection["files"])):
                raise Rejected("Selected subscription login digest differs.")
        deadline = datetime.fromisoformat(record["manifest"]["deadline"]).timestamp()
        if (
            not isinstance(grant, dict)
            or set(grant) != {"token", "expires_at"}
            or not isinstance(grant["token"], str)
            or not 32 <= len(grant["token"]) <= 128
        ):
            raise Rejected("Native operation grant is invalid.")
        expiry = grant["expires_at"]
        if (
            type(expiry) not in {int, float}
            or not math.isfinite(expiry)
            or not self.now() < expiry <= deadline
        ):
            raise Rejected(
                "Native operation grant is expired or exceeds the task deadline."
            )
        if any(
            contains_secret(
                (self.runtime.artifacts / record["manifest"][key]).read_bytes(),
                canonical(envelope),
            )
            for key in ("source_digest", "context_digest")
        ):
            raise Rejected(
                "Task source or context contains selected credential material."
            )
        state = {
            "scope": scope,
            "grant": grant,
            "provider": provider,
            "model": context["model"],
        }
        if provider in {"codex", "claude"}:
            state["original_files"] = connection["files"]
        path = self._path(operation_id)
        if path.exists():
            current = self._state(operation_id)
            if any(current.get(key) != value for key, value in state.items()):
                raise Conflict(
                    "Worker connection replay changed its selected material."
                )
        else:
            durable(path, canonical(state))
        return supplied_secret

    def network(self, operation_id, profile):
        scope = self._scope(operation_id)
        if profile.phase != scope["phase"]:
            raise Rejected("Network phase differs from the registered worker profile.")
        if profile.phase == "validation":
            return []
        if profile.phase == "agent":
            state = self._state(operation_id)
        else:
            state = {"scope": scope, "model": "", "grant": {"token": ""}}
        result = self.client.request(
            "/v4/connections/activate",
            {"scope": scope, "token": state["grant"]["token"], "model": state["model"]},
        )
        if (
            not isinstance(result.get("token"), str)
            or not 32 <= len(result["token"]) <= 128
            or result.get("provider") not in {"setup", "codex", "claude", "opencode"}
        ):
            raise Rejected("Runner returned an invalid phase grant.")
        expected_provider = state.get("provider", "setup")
        if result["provider"] != expected_provider:
            raise Conflict("Runner grant differs from the selected phase connection.")
        state.update(
            grant={"token": result["token"], "expires_at": result["expires_at"]},
            provider=result["provider"],
        )
        durable(self._path(operation_id), canonical(state))
        host = address(urlsplit(self.origin).hostname)
        return [(host, 3130 if result["provider"] == "opencode" else 3128)]

    def environment(self, operation_id, profile, environment):
        if profile.phase == "validation":
            return environment
        state = self._state(operation_id)
        host = address(urlsplit(self.origin).hostname)
        token = state["grant"]["token"]
        result = dict(environment)
        if state["provider"] == "opencode":
            for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
                result.pop(name, None)
            result.update(
                VIBEARR_OLLAMA_URL=f"http://{host}:3130/v1", VIBEARR_EGRESS_GRANT=token
            )
        else:
            proxy = f"http://grant:{token}@{host}:3128"
            result.update(
                HTTP_PROXY=proxy, HTTPS_PROXY=proxy, http_proxy=proxy, https_proxy=proxy
            )
        return result

    def finish(self, operation_id, home, record):
        path = self._path(operation_id)
        if not path.exists():
            return
        state = self._state(operation_id)
        data = state.get("return")
        if data is None:
            data = {
                "scope": state["scope"],
                "token": state["grant"]["token"],
                "terminated": True,
            }
            provider = state["provider"]
            if provider in {"codex", "claude"}:
                data["files"] = collect(provider, home)
            state["return"] = data
            durable(path, canonical(state))
        result = self.client.request("/v4/connections/finish", data)
        if result.get("finished") is not True:
            raise Conflict("Connection return is uncertain; keep the worker fenced.")
        path.unlink()

    def suspend(self, operation_id):
        if not self._path(operation_id).exists():
            return
        state = self._state(operation_id)
        return self.client.request(
            "/v4/connections/suspend",
            {"scope": state["scope"], "token": state["grant"]["token"]},
        )

    def revoke(self, operation_id):
        if not self._path(operation_id).exists():
            return
        state = self._state(operation_id)
        if (
            self.runtime.record(operation_id)["state"] == "prepared"
            and "return" not in state
        ):
            data = {
                "scope": state["scope"],
                "token": state["grant"]["token"],
                "terminated": True,
            }
            if "original_files" in state:
                data["files"] = state["original_files"]
            state["return"] = data
            durable(self._path(operation_id), canonical(state))
        return self.client.request(
            "/v4/connections/revoke",
            {"scope": state["scope"], "token": state["grant"]["token"]},
        )

    def resume(self, operation_id):
        if not self._path(operation_id).exists():
            return
        state = self._state(operation_id)
        return self.client.request(
            "/v4/connections/activate",
            {
                "scope": state["scope"],
                "token": state["grant"]["token"],
                "model": state["model"],
            },
        )

    def protected_material(self, secret, record):
        selected = json.loads(secret) if secret else {}
        values = {
            "selected": selected,
            "database_url": record.get("database_url", ""),
            "password": urlsplit(record.get("database_url", "")).password or "",
        }
        operation_id = record["manifest"]["operation_id"]
        if self._path(operation_id).exists():
            values["mediation"] = self._state(operation_id)
        provider = selected.get("connection", {}).get("provider")
        if provider in {"codex", "claude"}:
            try:
                values["refreshed"] = collect(provider, self.runtime.home)
            except (OSError, ValueError, KeyError):
                pass
        return canonical(values)
