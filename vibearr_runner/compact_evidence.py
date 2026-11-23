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


def protected_operation(record, worker, *, exit_code=0):
    manifest, result = record["manifest"], record["result"]
    sealed = {"manifest": manifest, "result": result, "boot": record["worker_boot"]}
    if record["state"] != "exited" or record["descendants_terminated"] is not True:
        raise ValueError("Operation cleanup is unverified.")
    if result["exit_code"] != exit_code or record["receipt_digest"] != digest(sealed):
        raise ValueError("Protected result or sealed receipt differs.")
    if (
        result["operation_id"] != manifest["operation_id"]
        or result["run_id"] != manifest["run_id"]
    ):
        raise ValueError("Protected result differs from its manifest.")
    for key in ("binding_digest", "instance_id", "worker_boot"):
        if record["execution_identity"][key] != worker[key]:
            raise ValueError("Operation belongs to another worker authority.")
    return manifest, result


def validate_document(name, document, source, worker, images):
    try:
        if (
            document["kind"] != "actual-" + name
            or document["source_revision"] != source
        ):
            raise ValueError("Document type or source differs.")
        if document["worker"] != worker or document["images"] != images:
            raise ValueError("Document authority or immutable images differ.")
        payload = document["payload"]
        if not isinstance(payload, dict) or not payload:
            raise ValueError("Underlying engineering proof is missing.")
        validate_payload(name, payload, source, worker, images)
    except (KeyError, TypeError, ValueError, AttributeError) as error:
        raise CompactEvidenceError(
            "Incomplete or failed typed engineering proof: " + name
        ) from error


def validate_payload(name, payload, source, worker, images):
    if name == "trusted_validation":
        from vibearr_executor.validation_profile import CHECK_IDS

        report = payload["report"]
        checks = report["result"]["validation"]["checks"]
        if report["commit_sha"] != source or report["result"]["state"] != "passed":
            raise ValueError
        if report["result"]["validation"]["image_digest"] != images["worker"]:
            raise ValueError
        if len(checks) != len(CHECK_IDS) or {entry["id"] for entry in checks} != set(
            CHECK_IDS
        ):
            raise ValueError
        records = {
            record["manifest"]["operation_id"]: record
            for record in payload["operations"]
        }
        for check in checks:
            manifest, result = protected_operation(
                records[check["operation_id"]], worker
            )
            if (
                manifest["source_digest"] != report["source_digest"]
                or manifest["phase"] != "validation"
            ):
                raise ValueError
            if result["log_digest"] != check["log_digest"] or any(
                result.get(key) for key in ("timed_out", "canceled", "export_error")
            ):
                raise ValueError
    elif name == "changed_dependency":
        from vibearr_executor.validation_profile import (
            ValidationProfile,
            validate_dependency_provenance,
        )

        build = payload["build"]
        profile = ValidationProfile.parse(build["profile"])
        parent = ValidationProfile.parse(payload["parent_profile"])
        provenance = validate_dependency_provenance(
            build["provenance"], profile, parent=parent, run_id=build["run_id"]
        )
        manifest, _result = protected_operation(payload["operation"], worker)
        if (
            build["state"] != "ready"
            or parent.dependency_digest == profile.dependency_digest
        ):
            raise ValueError
        if (
            manifest["source_digest"] != provenance["source_digest"]
            or manifest["phase"] != "validation"
        ):
            raise ValueError
    elif name == "resource_limits":
        boundary = payload["boundary"]
        resources = boundary["resources"]
        if (
            digest(boundary) != worker["qualification_digest"]
            or not boundary["receipts"]
        ):
            raise ValueError
        if (
            int(resources["memory_max"]) != 4096 * 1024**2
            or int(resources["pids_max"]) != 128
        ):
            raise ValueError
        if resources["oom_kill_after"] <= resources["oom_kill_before"]:
            raise ValueError
        protected_operation(payload["oom_operation"], worker, exit_code=-9)
        if payload["oom_operation"]["receipt_digest"] not in {
            entry["receipt_digest"] for entry in boundary["receipts"]
        }:
            raise ValueError
    elif name == "worker_restart":
        record = payload["operation"]
        result = record["result"]
        sealed = {
            "manifest": record["manifest"],
            "result": result,
            "boot": record["worker_boot"],
        }
        if (
            record["receipt_digest"] != digest(sealed)
            or payload["restart_receipt"] != record["receipt_digest"]
        ):
            raise ValueError
        if (
            result["worker_restart"] is not True
            or result["reconciled_boot"] != worker["worker_boot"]
        ):
            raise ValueError
        if (
            record["worker_boot"] == worker["worker_boot"]
            or record["descendants_terminated"] is not True
        ):
            raise ValueError
    elif name == "runner_restart":
        before, after = payload["before"], payload["after"]
        if before["Id"] != after["Id"] or {before["Image"], after["Image"]} != {
            images["runner"]
        }:
            raise ValueError
        if (
            before["State"]["StartedAt"] == after["State"]["StartedAt"]
            or after["State"]["Health"]["Status"] != "healthy"
        ):
            raise ValueError
        if (
            payload["controller_before"]["start_ticks"]
            == payload["controller_after"]["start_ticks"]
        ):
            raise ValueError
        if (
            payload["controller_before"]["runner_id"]
            != payload["controller_after"]["runner_id"]
        ):
            raise ValueError
    elif name == "backup_restore":
        manifest = payload["manifest"]
        volumes = [
            "application-secrets",
            "database-secrets",
            "runner-secrets",
            "runner-data",
            "runner-logins",
            "worker-secrets",
            "worker-control",
        ]
        if (
            manifest["schema_version"] != 3
            or manifest["runtime"] != "compact-worker-v1"
            or manifest["state_volumes"] != volumes
        ):
            raise ValueError
        if (
            not manifest["database"]["tables"]
            or manifest["database"]["tables"] != payload["restored_tables"]
        ):
            raise ValueError
        for table in payload["restored_tables"]:
            if not HEX.fullmatch(table["sha256"]):
                raise ValueError
        required = {
            "business": ("credential_decrypted", "queue_and_history_preserved"),
            "runner": (
                "old_token_rejected",
                "new_token_authenticated",
                "controller_journal_archived",
                "login_state_preserved",
            ),
            "worker": (
                "old_token_rejected",
                "new_token_authenticated",
                "mediation_token_rotated",
                "worker_control_archived",
            ),
        }
        for section, keys in required.items():
            if any(payload[section][key] is not True for key in keys):
                raise ValueError
        if {payload["runner"]["backup_id"], payload["worker"]["backup_id"]} != {
            manifest["backup_id"]
        }:
            raise ValueError
        if payload["runner"]["processing_enabled"] is not False:
            raise ValueError
    elif name == "cli_packaging":
        report = payload["report"]
        if (
            report["kind"] != "offline-container-packaging"
            or report["commit_sha"] != source
            or report["passed"] is not True
        ):
            raise ValueError
        if (
            report["exit_code"] != 0
            or report["native_receipt_version"] != 2
            or report["production_qualification"] is not False
        ):
            raise ValueError
        if not HEX.fullmatch(report["source_digest"]) or not payload["wheels"]:
            raise ValueError
        for wheel in payload["wheels"]:
            if (
                not HEX.fullmatch(wheel["sha256"])
                or wheel["metadata_verified"] is not True
            ):
                raise ValueError
    elif name == "browser":
        validate_browser(payload, source, worker)


def validate_browser(payload, source, worker):
    import base64
    import tempfile
    from pathlib import Path
    from vibearr_executor.artifacts import unpack
    from .browser_receipts import validate_worker_report

    report = payload["report"]
    archive = base64.b64decode(payload["archive"], validate=True)
    if (
        len(archive) > 16 * 1024**2
        or hashlib.sha256(archive).hexdigest() != payload["archive_sha256"]
    ):
        raise ValueError
    binding, observed = report["source_binding"], report["worker_binding"]
    if (
        binding["revision"] != source
        or observed["execution_identity"]["worker_boot"] != worker["worker_boot"]
    ):
        raise ValueError
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        for path, (_mode, content) in unpack(archive).items():
            target = root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        validate_worker_report(
            report, binding=binding, worker_binding=observed, storage_root=root
        )


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
            validate_document(name, receipt["document"], source, worker, images)
    return value
