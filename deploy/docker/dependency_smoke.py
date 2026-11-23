"""Exercise setup-to-validation artifact transfer without model credentials."""

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import time
from uuid import uuid4

from vibearr_executor.artifacts import pack
from vibearr_executor.integrated_validate import validation_connection
from vibearr_executor.contracts import canonical, digest
from vibearr_executor.runtime import durable
from vibearr_executor.validation_profile import (
    PACKAGE_HOSTS,
    ValidationProfile,
    validate_dependency_provenance,
)


def main():
    client, parent = validation_connection()
    run_id, build_id, operation = (str(uuid4()) for _ in range(3))
    deadline = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()
    source = pack(
        {
            "requirements.txt": (0o644, b"packaging==26.3\n"),
            "probe.py": (0o644, b"value = 1\n"),
        }
    )
    client.upload("source", source)
    client.prepare_dependencies(
        {
            "schema_version": 1,
            "run_id": run_id,
            "build_id": build_id,
            "source_digest": digest(source),
            "setup_profile_id": parent.setup_profile().profile_id,
            "validation_profile": parent.data(),
            "limits": parent.setup_profile().data()["limits"],
            "deadline": deadline,
            "network_hosts": list(PACKAGE_HOSTS),
        }
    )
    client.dependencies(build_id, "start")
    while True:
        result = client.dependencies(build_id, "status")
        if result["state"] == "ready":
            break
        if result["state"] != "running" or datetime.now(
            timezone.utc
        ) >= datetime.fromisoformat(deadline):
            client.dependencies(build_id, "stop")
            raise RuntimeError(
                "Dependency smoke setup failed: "
                + client.logs(build_id, 0, 8192).decode(errors="replace")
            )
        time.sleep(0.25)
    profile = ValidationProfile.parse(result["profile"])
    validate_dependency_provenance(
        result["provenance"], profile, parent=parent, run_id=run_id
    )
    fixed = profile.executor_profiles()[0]
    context = canonical(
        {
            "schema_version": 1,
            "kind": "validation",
            "validation_profile_digest": profile.digest,
        }
    )
    client.upload("context", context)
    manifest = {
        "schema_version": 1,
        "run_id": run_id,
        "operation_id": operation,
        "profile_id": fixed.profile_id,
        "profile_digest": fixed.digest,
        "source_digest": digest(source),
        "context_digest": digest(context),
        "phase": "validation",
        "limits": fixed.data()["limits"],
        "deadline": deadline,
        "network_hosts": [],
    }
    client.prepare(manifest)
    client.start(operation)
    while client.status(operation)["state"] == "running":
        if datetime.now(timezone.utc) >= datetime.fromisoformat(deadline):
            client.stop(operation)
            raise TimeoutError("Dependency validation smoke timed out.")
        time.sleep(0.25)
    observed = client.status(operation)
    if (
        not observed.get("descendants_terminated")
        or observed.get("result", {}).get("exit_code") != 0
    ):
        raise RuntimeError(
            "Dependency validation smoke failed: "
            + client.logs(operation, 0, 8192).decode(errors="replace")
        )
    proof = {
        "build": {**result, "run_id": run_id},
        "parent_profile": parent.data(),
        "operation": {**observed, "manifest": manifest},
    }
    durable(Path("/control/dependency-smoke-proof.json"), canonical(proof))
    client.purge_dependencies(build_id, run_id=run_id)
    print(
        json.dumps(
            {
                "kind": "integrated-dependency-smoke",
                "passed": True,
                "receipt_digest": observed["receipt_digest"],
            }
        )
    )


if __name__ == "__main__":
    main()
