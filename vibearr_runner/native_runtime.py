"""Installed executor diagnostics, independent of production-run or backend proof."""

from .policy import ExecutionPolicy


def runtime_diagnostics(policy: dict, client=None, *, validation_profile=None) -> dict:
    try:
        from vibearr_executor.client import Client, ExecutorUnavailable
        from vibearr_executor.contracts import Limits, Rejected, sha256
        from vibearr_executor import SLOT_PROTOCOL_VERSION, SLOT_QUALIFICATION_VERSION

        selected = ExecutionPolicy.from_data(policy)
        executor = client or Client.from_environment()
        capabilities = executor.capabilities()
        if (
            capabilities.get("schema_version") != 1
            or capabilities.get("protocol_version") != SLOT_PROTOCOL_VERSION
            or capabilities.get("qualification_version") != SLOT_QUALIFICATION_VERSION
        ):
            raise ValueError("Executor protocol version is unsupported.")
        sha256(capabilities.get("binding_digest"))
        if (
            capabilities.get("runtime")
            not in {"compose-slot-v2", "integrated-runner-v1"}
            or capabilities.get("qualified") is not True
        ):
            raise ValueError(
                "Installed executor boundary qualification is missing or stale."
            )
        if capabilities.get("accepting_work") is not True:
            raise ValueError("Executor storage capacity does not permit new work.")
        profiles = {
            profile["profile_id"]: profile
            for profile in capabilities.get("profiles", [])
        }
        agent = profiles.get(selected.runtime_profile)
        validation = profiles.get(selected.validation_profile)
        validation_document = None
        from vibearr_executor.validation_profile import PROFILE_ID

        if selected.validation_profile == PROFILE_ID:
            from .native_validation import validation_diagnostics

            diagnostic = validation_diagnostics(
                selected, executor, validation_profile, capabilities=capabilities
            )
            if not diagnostic["ready"]:
                raise ValueError(diagnostic["reason"])
            validation_document = diagnostic["profile"]
            first = validation_document["executor_profiles"][0]["profile_id"]
            validation = profiles[first]
        if (
            not agent
            or agent.get("phase") != "agent"
            or not validation
            or validation.get("phase") != "validation"
        ):
            raise ValueError("Selected agent/validation profiles are not installed.")
        for profile in (agent, validation):
            sha256(profile.get("profile_digest"))
            limits = profile["limits"]
            desired = {
                "cpu": selected.cpu_limit,
                "memory_mb": selected.memory_mb,
                "pids": selected.pids_limit,
                "workspace_mb": selected.workspace_mb,
            }
            Limits.parse(desired)
            if any(
                type(limits.get(key)) is not int or limits[key] != value
                for key, value in desired.items()
            ):
                raise ValueError(
                    "Selected profile cannot enforce the configured resource limits."
                )
        if not set(agent.get("network_hosts", [])) <= set(selected.network_hosts):
            raise ValueError(
                "Selected agent profile requires hosts outside the configured network policy."
            )
        if validation.get("network_hosts") or validation.get("backend_secret_env"):
            raise ValueError(
                "Installed validation profile must be offline and credential-free."
            )
        return {
            "runtime_ready": True,
            "native_agent_ready": False,
            "binding_digest": capabilities["binding_digest"],
            "runtime_profile": agent,
            "validation_profile": validation_document or validation,
            "reason": "Installed runtime is qualified; native adapter/authentication and lifecycle diagnostics remain separate.",
        }
    except ImportError:
        reason = "Install vibearr-runner[native-runtime] with the vibearr-executor client package."
    except (
        KeyError,
        ValueError,
        TypeError,
        OSError,
        ExecutorUnavailable,
        Rejected,
    ) as exc:
        reason = str(exc) or "Executor diagnostics are unavailable."
    return {"runtime_ready": False, "native_agent_ready": False, "reason": reason}
