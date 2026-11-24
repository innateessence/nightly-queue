"""Native process sessions are separate from model proposal backends."""

from dataclasses import asdict, dataclass
import json
import re
from typing import Protocol
from uuid import UUID


class NativeSessionError(ValueError):
    pass


@dataclass(frozen=True)
class NativeSession:
    adapter: str
    agent_version: str
    account: dict
    auth_kind: str
    image_digest: str
    runtime_binding_digest: str
    manifest: dict
    execution_identity: dict | None = None

    def to_data(self) -> dict:
        return json.loads(json.dumps(asdict(self)))

    @classmethod
    def from_data(cls, value: dict) -> "NativeSession":
        if isinstance(value, dict) and "execution_identity" not in value:
            value = {**value, "execution_identity": None}
        if not isinstance(value, dict) or set(value) != set(cls.__dataclass_fields__):
            raise NativeSessionError("Invalid native session record.")
        manifest = value.get("manifest")
        account = value.get("account")
        if not isinstance(manifest, dict) or not isinstance(account, dict):
            raise NativeSessionError("Invalid native session binding.")
        try:
            for key in ("run_id", "operation_id"):
                if str(UUID(manifest[key])) != manifest[key]:
                    raise ValueError
            for key in ("profile_digest", "source_digest", "context_digest"):
                if re.fullmatch(r"[a-f0-9]{64}", manifest[key]) is None:
                    raise ValueError
            if re.fullmatch(r"sha256:[a-f0-9]{64}", value["image_digest"]) is None:
                raise ValueError
            if re.fullmatch(r"[a-f0-9]{64}", value["runtime_binding_digest"]) is None:
                raise ValueError
            if set(account) != {
                "backend",
                "credential_id",
                "credential_version",
                "model",
            }:
                raise ValueError
            for key in ("credential_id", "credential_version"):
                if type(account[key]) is not int or account[key] < 1:
                    raise ValueError
            for text in (value["adapter"], value["agent_version"], account["model"]):
                if not isinstance(text, str) or not text or len(text) > 200:
                    raise ValueError
            if value["auth_kind"] not in {"token", "session", "connection", "login"}:
                raise ValueError
            if value["execution_identity"] is not None:
                from .native_contract import validate_execution_identity

                validate_execution_identity(value["execution_identity"])
        except (KeyError, TypeError, ValueError, AttributeError):
            raise NativeSessionError("Invalid native session binding.") from None
        return cls(**json.loads(json.dumps(value)))


class NativeAdapter(Protocol):
    def prepare(
        self,
        *,
        run_id: str,
        operation_id: str,
        source: bytes,
        prompt: str,
        model: str,
        policy: dict,
        deadline: str,
        credential: dict,
    ) -> NativeSession: ...

    def start(self, session: NativeSession) -> dict: ...

    def inspect(self, session: NativeSession) -> dict: ...

    def cancel(self, session: NativeSession) -> dict: ...

    def reconcile(self, session: NativeSession) -> dict: ...

    def events(self, session: NativeSession) -> list[dict]: ...

    def read_events(self, session: NativeSession) -> tuple[object, bytes]: ...

    def outcome(self, session: NativeSession, source: bytes) -> dict: ...

    def diagnostics(
        self, policy: dict, credential: dict, *, model=None, proof=None
    ) -> dict: ...

    def prepare_auth_probe(
        self,
        *,
        run_id: str,
        operation_id: str,
        model: str,
        policy: dict,
        deadline: str,
        credential: dict,
    ) -> NativeSession: ...

    def authentication_result(self, session: NativeSession) -> dict: ...


class NativeBackendRegistry:
    def create(self, provider: str, client) -> NativeAdapter:
        if (
            getattr(client, "integrated", False) is True
            or getattr(client, "compact", False) is True
        ):
            from .native_cli import NativeCLIAdapter

            return NativeCLIAdapter(client, provider)
        if provider == "codex":
            from .native_codex import NativeCodexAdapter

            return NativeCodexAdapter(client)
        raise NativeSessionError("Selected provider has no installed native adapter.")
