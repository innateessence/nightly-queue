"""Terminal payload removal retains immutable execution identities."""

from __future__ import annotations


from .contracts import (
    ArtifactRemoved,
    Rejected,
    canonical,
    digest,
)

RESOURCES = {"source", "logs"}


def resources(value) -> set[str]:
    if not isinstance(value, (list, tuple)) or not value:
        raise Rejected("Select source and/or logs for retention.")
    if any(not isinstance(item, str) or item not in RESOURCES for item in value):
        raise Rejected("Unknown retention resource.")
    if len(set(value)) != len(value):
        raise Rejected("Retention resources must be unique.")
    return set(value)


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
        "execution_identity",
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
