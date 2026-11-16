import hashlib
import json
import re
from dataclasses import asdict, dataclass
from typing import Any
from uuid import UUID, uuid4


class ApprovalRequired(Exception):
    pass


def digest(value: Any) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class ActionSpec:
    argv: tuple[str, ...]
    mode: str
    source_kind: str
    source_reference: str
    source_digest: str
    workspace_id: str
    base_revision: str
    content_digest: str

    @classmethod
    def from_data(cls, data: Any) -> "ActionSpec":
        if not isinstance(data, dict) or set(data) != set(cls.__dataclass_fields__):
            raise ValueError(
                "Supply the complete action binding and no additional fields."
            )
        argv = data["argv"]
        if not isinstance(argv, list) or not 1 <= len(argv) <= 128:
            raise ValueError("Supply 1 to 128 command arguments.")
        for value in argv:
            if not isinstance(value, str) or "\0" in value or len(value) > 8192:
                raise ValueError(
                    "Command arguments must be bounded strings without NUL."
                )
        if not argv[0].startswith(
            ("/usr/bin/", "/usr/local/bin/", "/bin/", "/workspace/")
        ):
            raise ValueError("Use an absolute executable path inside the sandbox.")
        if data["mode"] not in {"exec", "shell"}:
            raise ValueError("Choose exec or shell interpretation.")
        if data["mode"] == "shell" and (
            len(argv) != 3 or argv[:2] != ["/bin/sh", "-c"]
        ):
            raise ValueError(
                "Shell interpretation requires exactly /bin/sh -c and the script."
            )
        shells = {"sh", "bash", "dash", "zsh", "ksh", "fish"}
        if data["mode"] == "exec" and argv[0].split("/")[-1] in shells:
            raise ValueError("Shell commands require explicit shell interpretation.")
        sources = {"issue", "comment", "repository", "research", "plan", "operator"}
        if data["source_kind"] not in sources:
            raise ValueError("Choose a supported provenance kind.")
        reference = data["source_reference"]
        if not isinstance(reference, str) or not reference or len(reference) > 2000:
            raise ValueError("Supply a bounded source reference.")
        for field in ("source_digest", "content_digest"):
            if not isinstance(data[field], str) or not re.fullmatch(
                r"[0-9a-f]{64}", data[field]
            ):
                raise ValueError("Supply SHA-256 source and workspace content digests.")
        revision = data["base_revision"]
        if not isinstance(revision, str) or not re.fullmatch(
            r"[0-9a-f]{40}|[0-9a-f]{64}", revision
        ):
            raise ValueError("Supply the inspected base revision.")
        try:
            workspace_id = str(UUID(data["workspace_id"]))
        except (ValueError, TypeError, AttributeError):
            raise ValueError("Supply a workspace UUID.") from None
        return cls(**{**data, "argv": tuple(argv), "workspace_id": workspace_id})

    def to_data(self) -> dict[str, Any]:
        return {**asdict(self), "argv": list(self.argv)}

    @property
    def binding_digest(self) -> str:
        return digest(self.to_data())


class ApprovalBroker:
    def __init__(self, client, run_id: str, claim_token: str):
        self.client = client
        self.run_id = str(UUID(run_id))
        self.claim_token = claim_token

    def request(self, action: ActionSpec, operation_id: str) -> dict[str, Any]:
        payload = {
            "claim_token": self.claim_token,
            "operation_id": str(UUID(operation_id)),
            "action": action.to_data(),
        }
        return self.client.request(
            "POST", f"/api/v1/runner/runs/{self.run_id}/approvals/", payload
        )["approval"]

    def consume(self, approval_id: str, action: ActionSpec) -> str:
        approval_id = str(UUID(approval_id))
        execution_id = str(uuid4())
        payload = {
            "claim_token": self.claim_token,
            "binding_digest": action.binding_digest,
            "execution_id": execution_id,
        }
        path = f"/api/v1/runner/runs/{self.run_id}/approvals/{approval_id}/consume/"
        response = self.client.request("POST", path, payload)
        if (
            response.get("permit") is not True
            or response.get("execution_id") != execution_id
        ):
            raise ApprovalRequired(
                "An unused human approval for this exact action is required."
            )
        return execution_id
