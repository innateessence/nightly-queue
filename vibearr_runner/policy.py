"""Supervisor-owned native execution policy."""

import hashlib
import ipaddress
import json
import re
from dataclasses import asdict, dataclass
from typing import Any


class PolicyError(ValueError):
    pass


@dataclass(frozen=True)
class ExecutionPolicy:
    schema_version: int = 1
    execution_kind: str = "native"
    workspace_access: str = "write"
    runtime_profile: str = "native-default"
    validation_profile: str = "vibearr-python312-v1"
    network_hosts: tuple[str, ...] = (
        "api.openai.com",
        "chatgpt.com",
        "auth.openai.com",
        "api.anthropic.com",
        "claude.ai",
        "platform.claude.com",
        "pypi.org",
        "files.pythonhosted.org",
    )
    cpu_limit: int = 2
    memory_mb: int = 4096
    pids_limit: int = 128
    workspace_mb: int = 2048
    repair_limit: int = 2
    delivery_mode: str = "pull_request"

    @classmethod
    def from_data(cls, value: Any) -> "ExecutionPolicy":
        if not isinstance(value, dict) or set(value) != set(cls.__dataclass_fields__):
            raise PolicyError("Supply the complete versioned execution policy.")
        if type(value["schema_version"]) is not int or value["schema_version"] != 1:
            raise PolicyError("Execution policy version is unsupported.")
        if value["execution_kind"] != "native" or value["workspace_access"] != "write":
            raise PolicyError("Only isolated native workspace execution is supported.")
        for key in ("runtime_profile", "validation_profile"):
            if not isinstance(value[key], str) or not re.fullmatch(
                r"[a-z0-9][a-z0-9._-]{0,99}", value[key]
            ):
                raise PolicyError("Choose a registered runtime and validation profile.")
        bounds = {
            "cpu_limit": (1, 64),
            "memory_mb": (128, 262144),
            "pids_limit": (16, 4096),
            "workspace_mb": (64, 1048576),
            "repair_limit": (0, 10),
        }
        for key, (lower, upper) in bounds.items():
            if type(value[key]) is not int or not lower <= value[key] <= upper:
                raise PolicyError("Execution resource or repair limit is out of range.")
        if value["delivery_mode"] not in ("pull_request", "merge"):
            raise PolicyError("Choose pull request or merge delivery.")
        hosts = value["network_hosts"]
        if not isinstance(hosts, list) or len(hosts) > 64:
            raise PolicyError("Supply at most 64 explicit network hosts.")
        for host in hosts:
            if not isinstance(host, str) or not re.fullmatch(
                r"[a-z0-9]+(?:[.-][a-z0-9]+)*\.[a-z]{2,63}", host
            ):
                raise PolicyError("Network hosts must be explicit public DNS names.")
            if host.endswith((".localhost", ".local", ".internal")):
                raise PolicyError("Private network hosts are not permitted.")
            try:
                ipaddress.ip_address(host)
            except ValueError:
                continue
            raise PolicyError("Network addresses are not permitted.")
        if len(set(hosts)) != len(hosts):
            raise PolicyError("Network hosts must be unique.")
        return cls(**{**value, "network_hosts": tuple(hosts)})

    def to_data(self) -> dict[str, Any]:
        value = asdict(self)
        value["network_hosts"] = list(self.network_hosts)
        return value

    @property
    def digest(self) -> str:
        encoded = json.dumps(self.to_data(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode()).hexdigest()


def default_execution_policy() -> dict[str, Any]:
    return ExecutionPolicy().to_data()
