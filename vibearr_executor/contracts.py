import hashlib
import ipaddress
import json
import math
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

MAX_ARCHIVE = 32 * 1024 * 1024
MAX_CONTEXT = 1024 * 1024
MAX_FILES = 10000
MAX_LOG = 1024 * 1024
MAX_RESULT = 20 * 1024 * 1024


class Rejected(ValueError):
    pass


class Conflict(Rejected):
    pass


class ArtifactRemoved(Conflict):
    pass


class NotFound(Rejected):
    pass


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def identifier(value: Any) -> str:
    if not isinstance(value, str) or str(UUID(value)) != value:
        raise Rejected("Identity must be a canonical UUID.")
    return value


def sha256(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value):
        raise Rejected("Expected a SHA-256 digest.")
    return value


def public_host(host: str) -> bool:
    if not isinstance(host, str) or len(host) > 253:
        return False
    pattern = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+"
    if not re.fullmatch(pattern, host):
        return False
    if host.endswith((".local", ".internal", ".localhost")):
        return False
    try:
        ipaddress.ip_address(host)
        return False
    except ValueError:
        return True


@dataclass(frozen=True)
class Limits:
    cpu: int = 1
    memory_mb: int = 512
    pids: int = 64
    workspace_mb: int = 64

    @classmethod
    def parse(cls, data: Any) -> "Limits":
        if not isinstance(data, dict) or set(data) != set(cls.__dataclass_fields__):
            raise Rejected("Supply exact resource limits.")
        for key, upper in {
            "cpu": 64,
            "memory_mb": 262144,
            "pids": 4096,
            "workspace_mb": 16384,
        }.items():
            if type(data[key]) is not int or not 1 <= data[key] <= upper:
                raise Rejected("Invalid resource limit.")
        if data["memory_mb"] < data["workspace_mb"] + 128 or data["pids"] < 16:
            raise Rejected(
                "Memory must cover workspace and runtime overhead; require at least 16 PIDs."
            )
        return cls(**data)


@dataclass(frozen=True)
class Profile:
    profile_id: str
    version: int
    image: str
    phase: str
    argv: tuple[str, ...]
    limits: Limits
    network_hosts: tuple[str, ...] = ()
    backend_secret_env: str = ""
    database_image: str = ""

    @classmethod
    def parse(cls, data: Any) -> "Profile":
        if not isinstance(data, dict) or set(data) != set(cls.__dataclass_fields__):
            raise Rejected("Invalid registered profile.")
        if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,99}", data["profile_id"]):
            raise Rejected("Invalid profile ID.")
        if type(data["version"]) is not int or data["version"] < 1:
            raise Rejected("Invalid profile version.")
        for key in ("image", "database_image"):
            if key == "database_image" and not data[key]:
                continue
            if not re.fullmatch(r"sha256:[a-f0-9]{64}", data[key]):
                raise Rejected(
                    "Profiles require locally installed immutable image IDs."
                )
        if data["phase"] not in {"agent", "setup", "validation"}:
            raise Rejected("Unknown runtime phase.")
        argv = data["argv"]
        if not isinstance(argv, list) or not argv or len(argv) > 64:
            raise Rejected("Invalid fixed launch recipe.")
        if not all(
            isinstance(arg, str) and "\x00" not in arg and len(arg) < 4096
            for arg in argv
        ):
            raise Rejected("Invalid fixed launch argument.")
        hosts = data["network_hosts"]
        if (
            not isinstance(hosts, list)
            or len(hosts) > 64
            or not all(public_host(h) for h in hosts)
        ):
            raise Rejected("Invalid egress allowlist.")
        secret = data["backend_secret_env"]
        if secret and (
            data["phase"] != "agent"
            or not re.fullmatch(r"[A-Z][A-Z0-9_]{1,63}", secret)
        ):
            raise Rejected("Only agent profiles can select a backend credential.")
        if data["phase"] == "validation" and hosts:
            raise Rejected("Validation must have no external network.")
        return cls(
            **{
                **data,
                "argv": tuple(argv),
                "network_hosts": tuple(hosts),
                "limits": Limits.parse(data["limits"]),
            }
        )

    def data(self) -> dict:
        result = asdict(self)
        result.update(argv=list(self.argv), network_hosts=list(self.network_hosts))
        return result

    @property
    def digest(self) -> str:
        return digest(canonical(self.data()))


@dataclass(frozen=True)
class Manifest:
    schema_version: int
    run_id: str
    operation_id: str
    profile_id: str
    profile_digest: str
    source_digest: str
    context_digest: str
    phase: str
    limits: Limits
    deadline: str
    network_hosts: tuple[str, ...]

    @classmethod
    def parse(cls, data: Any, profile: Profile) -> "Manifest":
        return cls._parse(data, profile, datetime.now(timezone.utc))

    @classmethod
    def parse_archived(cls, data: Any, profile: Profile, started_at: Any) -> "Manifest":
        """Validate only after the completed record's receipt and identity checks."""
        return cls._parse_observed(data, profile, started_at)

    @classmethod
    def parse_reconciled(cls, data: Any, profile: Profile, ended_at: Any) -> "Manifest":
        """Validate only an authenticated restart receipt without a recorded start."""
        return cls._parse_observed(data, profile, ended_at)

    @classmethod
    def _parse_observed(
        cls, data: Any, profile: Profile, observed_at: Any
    ) -> "Manifest":
        if (
            type(observed_at) not in (int, float)
            or observed_at <= 0
            or observed_at >= 253402300800
            or not math.isfinite(observed_at)
        ):
            raise Rejected(
                "Historical validation requires a sealed observation timestamp."
            )
        try:
            admitted = datetime.fromtimestamp(observed_at, timezone.utc)
        except (ValueError, OverflowError, OSError):
            raise Rejected("Historical operation timestamp is invalid.") from None
        return cls._parse(data, profile, admitted)

    @classmethod
    def _parse(cls, data: Any, profile: Profile, admitted: datetime) -> "Manifest":
        if not isinstance(data, dict) or set(data) != set(cls.__dataclass_fields__):
            raise Rejected(
                "Supply the complete versioned job manifest; commands and mounts are forbidden."
            )
        if type(data["schema_version"]) is not int or data["schema_version"] != 1:
            raise Rejected("Unsupported job schema.")
        identifier(data["run_id"])
        identifier(data["operation_id"])
        for key in ("profile_digest", "source_digest", "context_digest"):
            sha256(data[key])
        if (data["profile_id"], data["profile_digest"], data["phase"]) != (
            profile.profile_id,
            profile.digest,
            profile.phase,
        ):
            raise Rejected("Job does not match its registered immutable profile.")
        limits = Limits.parse(data["limits"])
        if any(
            value > asdict(profile.limits)[key] for key, value in asdict(limits).items()
        ):
            raise Rejected("Requested resources exceed the registered profile.")
        try:
            deadline = datetime.fromisoformat(data["deadline"])
            remaining = (deadline - admitted).total_seconds()
        except (TypeError, ValueError):
            raise Rejected("Deadline must be a timezone-aware ISO timestamp.") from None
        if not 0 < remaining <= 3600:
            raise Rejected("Job deadline must be within the next hour.")
        hosts = data["network_hosts"]
        if not isinstance(hosts, list) or not set(hosts) <= set(profile.network_hosts):
            raise Rejected("Job cannot expand profile egress.")
        return cls(**{**data, "limits": limits, "network_hosts": tuple(hosts)})

    def data(self) -> dict:
        result = asdict(self)
        result["network_hosts"] = list(self.network_hosts)
        return result
