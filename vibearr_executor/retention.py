"""Terminal payload removal retains immutable execution identities."""

from __future__ import annotations

from datetime import datetime, timezone
import shutil
from typing import TYPE_CHECKING

from .contracts import (
    ArtifactRemoved,
    Conflict,
    Rejected,
    canonical,
    digest,
    identifier,
)

if TYPE_CHECKING:
    from .runtime import Runtime

RESOURCES = {"source", "logs"}


def resources(value) -> set[str]:
    if not isinstance(value, (list, tuple)) or not value:
        raise Rejected("Select source and/or logs for retention.")
    if any(not isinstance(item, str) or item not in RESOURCES for item in value):
        raise Rejected("Unknown retention resource.")
    if len(set(value)) != len(value):
        raise Rejected("Retention resources must be unique.")
    return set(value)


def fully_purged(record: dict) -> bool:
    return (
        record.get("tombstone") is True
        and set(record.get("resources_removed", [])) == RESOURCES
    )


def removed(record: dict, resource: str) -> bool:
    pending = record.get("purge_intent", [])
    return resource in record.get("resources_removed", []) or resource in pending


def require_available(record: dict, resource: str) -> None:
    if removed(record, resource):
        raise ArtifactRemoved("Executor artifact was removed by retention.")


def compact_result(record: dict) -> None:
    result = record.get("result", {})
    record.setdefault("receipt_digest", digest(canonical(result)))
    keep = {
        "schema_version",
        "run_id",
        "operation_id",
        "exit_code",
        "terminated",
        "termination",
        "timed_out",
        "canceled",
        "started_at",
        "ended_at",
        "log_digest",
        "log_bytes",
        "log_truncated",
        "artifact_digests",
        "dependency_artifact_digest",
    }
    summary = {key: value for key, value in result.items() if key in keep}
    artifact = result.get("artifact", {})
    if artifact:
        summary["artifact_digests"] = {
            "source_digest": artifact["source_digest"],
            "result_digest": artifact["result_digest"],
            "manifest_digest": digest(canonical(artifact)),
        }
    dependency = result.get("dependency_artifact")
    if dependency:
        summary["dependency_artifact_digest"] = digest(canonical(dependency))
    record["result"] = summary


def artifact_referenced(runtime: Runtime, content_digest: str) -> bool:
    for path in runtime.operations.iterdir():
        record = runtime.record(path.name)
        if removed(record, "source"):
            continue
        manifest = record.get("manifest", {})
        if content_digest in (
            manifest.get("source_digest"),
            manifest.get("context_digest"),
        ):
            return True
    for path in runtime.dependency_builds.directory.iterdir():
        record = runtime.dependency_builds.read(path.name)
        if (
            not removed(record, "source")
            and record["request"]["source_digest"] == content_digest
        ):
            return True
    return False


def purge(runtime: Runtime, operation_id: str, run_id: str, selected) -> dict:
    from .runtime import durable

    identifier(run_id)
    requested = resources(selected)
    with runtime.lock:
        record = runtime.record(operation_id)
        if record.get("manifest", {}).get("run_id") != run_id:
            raise Conflict("Retention run does not own this operation.")
        if (
            record["state"] != "exited"
            or record.get("descendants_terminated") is not True
        ):
            raise Conflict("Only confirmed terminal operations may be purged.")
        done = set(record.get("resources_removed", []))
        pending = (requested | set(record.get("purge_intent", []))) - done
        if not pending:
            return runtime.public(record)
        record["purge_intent"] = sorted(pending)
        record.setdefault("receipt_digest", digest(canonical(record.get("result", {}))))
        runtime.save(record)
        directory = runtime.path(operation_id)
        if "source" in pending:
            for name in ("input", "source.tar", "dependencies.tar"):
                path = directory / name
                if path.is_dir():
                    shutil.rmtree(path)
                else:
                    path.unlink(missing_ok=True)
            compact_result(record)
            durable(directory / "result.json", canonical(record["result"]))
        if "logs" in pending:
            (directory / "logs.bin").unlink(missing_ok=True)
        if "source" in pending:
            for key in ("source_digest", "context_digest"):
                content_digest = record["manifest"][key]
                if not artifact_referenced(runtime, content_digest):
                    (runtime.artifacts / content_digest).unlink(missing_ok=True)
        done.update(pending)
        record["resources_removed"] = sorted(done)
        record["purge_intent"] = []
        record["tombstone"] = done == RESOURCES
        record.setdefault("removed_at", {}).update(
            {key: datetime.now(timezone.utc).isoformat() for key in pending}
        )
        runtime.save(record)
        return runtime.public(record)
