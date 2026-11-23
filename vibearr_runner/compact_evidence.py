"""Validate compact engineering evidence against current worker authority."""

import hashlib
import json
import re
from uuid import UUID


SERVICES = ("app", "postgres", "runner", "worker")
EVIDENCE = (
    "trusted_validation",
    "changed_dependency",
    "browser",
    "backup_restore",
    "resource_limits",
    "runner_restart",
    "worker_restart",
    "cli_packaging",
)
ROLE_NAMES = (
    "intake",
    "specification",
    "build",
    "reviewer-code",
    "reviewer-security",
    "reviewer-ux",
    "reviewer-ui",
    "decision",
)
HEX = re.compile(r"[a-f0-9]{64}\Z")
IMAGE = re.compile(r"sha256:[a-f0-9]{64}\Z")


class CompactEvidenceError(ValueError):
    pass


def digest(value):
    content = json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()
    return hashlib.sha256(content).hexdigest()


def validate_compact_report(value, capabilities):
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != 1
        or value.get("kind") != "compact-stack-engineering"
        or value.get("runtime") != "compact-worker-v1"
        or value.get("passed") is not True
        or value.get("fixtures") is not False
        or value.get("services") != list(SERVICES)
        or value.get("runtime_service_count") != len(SERVICES)
        or value.get("extra_runtime_services") != []
    ):
        raise CompactEvidenceError(
            "Engineering evidence must bind the actual four-container runtime."
        )
    source = value.get("source_revision", "")
    if not isinstance(source, str) or not re.fullmatch(r"[a-f0-9]{40}", source):
        raise CompactEvidenceError(
            "Engineering evidence lacks an exact source revision."
        )
    images, containers = value.get("images"), value.get("containers")
    if not isinstance(images, dict) or set(images) != set(SERVICES):
        raise CompactEvidenceError("Engineering evidence omits a runtime image.")
    if not isinstance(containers, dict) or set(containers) != set(SERVICES):
        raise CompactEvidenceError("Engineering evidence omits a runtime container.")
    if any(
        not isinstance(image, str) or not IMAGE.fullmatch(image)
        for image in images.values()
    ):
        raise CompactEvidenceError("Engineering image identity is invalid.")
    if any(
        not isinstance(identity, str) or not HEX.fullmatch(identity)
        for identity in containers.values()
    ):
        raise CompactEvidenceError("Engineering container identity is invalid.")
    if len(set(containers.values())) != len(SERVICES):
        raise CompactEvidenceError(
            "Runtime services cannot share the same container identity."
        )
    worker = value.get("worker", {})
    if (
        not isinstance(capabilities, dict)
        or capabilities.get("runtime") != "compact-worker-v1"
        or capabilities.get("qualified") is not True
        or not isinstance(worker, dict)
    ):
        raise CompactEvidenceError(
            "Current authenticated worker qualification is required."
        )
    for name in (
        "binding_digest",
        "instance_id",
        "worker_boot",
        "image_digest",
        "qualification_digest",
    ):
        if worker.get(name) != capabilities.get(name) or not worker.get(name):
            raise CompactEvidenceError(
                "Engineering evidence differs from current worker authority."
            )
    if worker["image_digest"] != images["worker"]:
        raise CompactEvidenceError(
            "Worker qualification image differs from the observed worker."
        )
    for name in ("binding_digest", "qualification_digest"):
        if not isinstance(worker[name], str) or not HEX.fullmatch(worker[name]):
            raise CompactEvidenceError("Worker evidence digest is invalid.")
    for name in ("instance_id", "worker_boot"):
        try:
            if str(UUID(worker[name])) != worker[name]:
                raise ValueError
        except (ValueError, TypeError, AttributeError) as error:
            raise CompactEvidenceError(
                "Worker evidence identity is invalid."
            ) from error
    boundary = value.get("worker_boundary", {})
    if (
        not isinstance(boundary, dict)
        or boundary.get("kind") != "actual-worker-boundary"
        or boundary.get("runtime") != "compact-worker-v1"
        or boundary.get("passed") is not True
        or boundary.get("fixtures") is not False
        or digest(boundary) != worker["qualification_digest"]
    ):
        raise CompactEvidenceError(
            "Engineering evidence lacks the current actual boundary report."
        )
    for name in ("binding_digest", "instance_id", "worker_boot", "image_digest"):
        if boundary.get(name) != worker[name]:
            raise CompactEvidenceError("Worker boundary report has stale authority.")
    roles = capabilities.get("factory_roles", {})
    if not isinstance(roles, dict) or not set(ROLE_NAMES) <= set(roles):
        raise CompactEvidenceError("Actual read-only/build role proofs are incomplete.")
    for role in ROLE_NAMES:
        entry = roles[role]
        if not isinstance(entry, dict):
            raise CompactEvidenceError("Role qualification is malformed.")
        proof = entry.get("qualification", {})
        expected = "read_write" if role == "build" else "read_only"
        if (
            not isinstance(proof, dict)
            or entry.get("source_access") != expected
            or proof.get("kind") != "worker-boundary"
            or proof.get("role") != role
            or proof.get("source_access") != expected
            or proof.get("passed") is not True
            or proof.get("runtime_binding_digest") != worker["binding_digest"]
            or proof.get("worker_boot") != worker["worker_boot"]
            or proof.get("instance_id") != worker["instance_id"]
            or proof.get("image_digest") != worker["image_digest"]
        ):
            raise CompactEvidenceError(
                "Role qualification does not bind current worker access."
            )
        checks = proof.get("checks", {})
        access = "candidate_write_only" if role == "build" else "source_write_denied"
        required = (
            access,
            "control_paths_denied",
            "credentials_scoped",
            "descendants_terminated",
        )
        if not isinstance(checks, dict) or any(
            checks.get(name) is not True for name in required
        ):
            raise CompactEvidenceError(
                "Role qualification lacks its actual permission probes."
            )
    evidence = value.get("evidence", {})
    if not isinstance(evidence, dict) or set(evidence) != set(EVIDENCE):
        raise CompactEvidenceError("Engineering evidence is incomplete.")
    for name in EVIDENCE:
        observation = evidence[name]
        if (
            not isinstance(observation, dict)
            or observation.get("passed") is not True
            or observation.get("fixtures") is not False
            or observation.get("source_revision") != source
            or not observation.get("kind", "").startswith("actual-")
            or not isinstance(observation.get("receipts"), list)
            or not observation["receipts"]
        ):
            raise CompactEvidenceError(
                "Engineering observation lacks actual source-bound receipts: " + name
            )
        for receipt in observation["receipts"]:
            if (
                not isinstance(receipt, dict)
                or not HEX.fullmatch(receipt.get("sha256", ""))
                or not isinstance(receipt.get("document"), dict)
                or digest(receipt["document"]) != receipt["sha256"]
            ):
                raise CompactEvidenceError(
                    "Engineering observation has an invalid receipt digest."
                )
    return value
