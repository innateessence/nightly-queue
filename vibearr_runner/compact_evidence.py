"""Validate compact engineering evidence against current worker authority."""

import hashlib
import json
import math
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
    if (
        type(result["exit_code"]) is not int
        or result["exit_code"] != exit_code
        or record["receipt_digest"] != digest(sealed)
    ):
        raise ValueError("Protected result or sealed receipt differs.")
    if (
        record["worker_boot"] != worker["worker_boot"]
        or result["execution_identity"] != record["execution_identity"]
    ):
        raise ValueError("Protected result boot differs from current authority.")
    if record["execution_identity"]["runtime"] != worker.get(
        "runtime", "compact-worker-v1"
    ):
        raise ValueError("Protected operation belongs to another runtime.")
    if (
        result.get("terminated") is not True
        or any(
            result[key] is not False
            for key in ("timed_out", "canceled", "log_truncated")
        )
        or any(result.get(key) for key in ("error", "export_error"))
    ):
        raise ValueError(
            "Protected operation did not complete successfully with confirmed cleanup."
        )
    times = (result.get("started_at"), result.get("ended_at"))
    if (
        any(
            type(value) not in (int, float)
            or not 0 < value < 253402300800
            or not math.isfinite(value)
            for value in times
        )
        or times[0] > times[1]
    ):
        raise ValueError("Protected operation has invalid sealed start/end chronology.")
    if (
        result["operation_id"] != manifest["operation_id"]
        or record["operation_id"] != manifest["operation_id"]
        or result["run_id"] != manifest["run_id"]
    ):
        raise ValueError("Protected result differs from its manifest.")
    for key in ("binding_digest", "instance_id", "worker_boot"):
        if record["execution_identity"][key] != worker[key]:
            raise ValueError("Operation belongs to another worker authority.")
    return manifest, result


def validate_document(name, document, source, worker, images, containers=None):
    try:
        if (
            document["kind"] != "actual-" + name
            or document["source_revision"] != source
        ):
            raise ValueError("Document type or source differs.")
        if document["worker"] != worker or document["images"] != images:
            raise ValueError("Document authority or immutable images differ.")
        if document["runtime"] != "compact-worker-v1":
            raise ValueError("Engineering document belongs to another runtime.")
        payload = document["payload"]
        if not isinstance(payload, dict) or not payload:
            raise ValueError("Underlying engineering proof is missing.")
        if (
            name in {"trusted_validation", "cli_packaging"}
            and payload["report"]["source_digest"] != document["source_digest"]
        ):
            raise ValueError(
                "Underlying proof differs from the exact candidate archive."
            )
        if (
            name == "trusted_validation"
            and payload["report"]["tree_sha"] != document["tree_sha"]
        ):
            raise ValueError(
                "Trusted validation differs from the exact candidate tree."
            )
        if name == "browser" and any(
            payload["report"]["source_binding"][key] != document[key]
            for key in ("source_digest", "tree_sha")
        ):
            raise ValueError(
                "Browser evidence differs from the exact candidate archive or tree."
            )
        validate_payload(name, payload, source, worker, images, containers)
    except (KeyError, TypeError, ValueError, AttributeError) as error:
        raise CompactEvidenceError(
            "Incomplete or failed typed engineering proof: " + name
        ) from error


def boundary_proof(boundary, authority):
    from vibearr_executor.compact_profiles import REQUIRED_BOUNDARY_CHECKS

    if (
        boundary["kind"] != "actual-worker-boundary"
        or boundary["runtime"] != "compact-worker-v1"
    ):
        raise ValueError("Use an actual compact worker boundary report.")
    if boundary["passed"] is not True or boundary["fixtures"] is not False:
        raise ValueError("The actual worker boundary did not pass.")
    if digest(boundary) != authority["qualification_digest"] or boundary[
        "checks"
    ] != sorted(REQUIRED_BOUNDARY_CHECKS):
        raise ValueError(
            "Worker boundary receipt differs from current qualified authority."
        )
    for key in ("binding_digest", "instance_id", "worker_boot", "image_digest"):
        if boundary[key] != authority[key]:
            raise ValueError("Worker boundary has stale identity or image provenance.")
    if not HEX.fullmatch(boundary["restart_receipt"]) or not boundary["receipts"]:
        raise ValueError("Worker boundary lacks actual restart or execution receipts.")
    for receipt in boundary["receipts"]:
        if str(UUID(receipt["operation_id"])) != receipt[
            "operation_id"
        ] or not HEX.fullmatch(receipt["receipt_digest"]):
            raise ValueError("Worker boundary contains an invalid protected receipt.")


def fixed_probe(record, worker, case, *, reconciled=False):
    from vibearr_executor.contracts import Manifest, Profile
    from vibearr_executor.integrated_profiles import LIMITS, MODEL_HOSTS
    from vibearr_executor.slot_qualification import ARGV

    profile = Profile(
        "worker-qualification",
        1,
        worker["image_digest"],
        "agent",
        ARGV,
        LIMITS,
        MODEL_HOSTS,
        "",
        worker["image_digest"],
    )
    manifest = record["manifest"]
    if reconciled:
        Manifest.parse_reconciled(manifest, profile, record["result"].get("ended_at"))
    else:
        Manifest.parse_archived(manifest, profile, record["result"].get("started_at"))
    if manifest["context_digest"] != digest({"case": case}):
        raise ValueError("Protected qualification operation used another fixed probe.")


def validate_payload(name, payload, source, worker, images, containers=None):
    if name == "trusted_validation":
        from vibearr_executor.validation_profile import CHECK_IDS, ValidationProfile
        from vibearr_executor.contracts import Manifest

        report = payload["report"]
        checks = report["result"]["validation"]["checks"]
        profile = ValidationProfile.parse(report["profile"])
        expected = dict(zip(CHECK_IDS, profile.executor_profiles()))
        if report["commit_sha"] != source or report["result"]["state"] != "passed":
            raise ValueError
        if report["result"]["validation"]["image_digest"] != images["worker"]:
            raise ValueError
        if (
            profile.image != images["worker"]
            or report["result"]["validation"]["profile_digest"] != profile.digest
        ):
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
            Manifest.parse_archived(
                manifest, expected[check["id"]], result["started_at"]
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
        import base64
        from vibearr_executor.dependency_artifacts import manifest as artifact_manifest
        from vibearr_executor.contracts import Manifest
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
        manifest, validation_result = protected_operation(payload["operation"], worker)
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
        if (
            profile.image != images["worker"]
            or parent.image != images["worker"]
            or manifest["run_id"] != build["run_id"]
        ):
            raise ValueError
        fixed = profile.executor_profiles()[0]
        Manifest.parse_archived(manifest, fixed, validation_result["started_at"])
        if (
            provenance["setup_instance_id"] != worker["instance_id"]
            or provenance["setup_worker_boot"] != worker["worker_boot"]
            or provenance["parent_runtime_binding"] != worker["binding_digest"]
        ):
            raise ValueError
        setup, result = protected_operation(payload["setup_operation"], worker)
        Manifest.parse_archived(setup, parent.setup_profile(), result["started_at"])
        if (
            setup["operation_id"] != provenance["setup_operation_id"]
            or setup["run_id"] != build["run_id"]
            or setup["source_digest"] != provenance["source_digest"]
        ):
            raise ValueError
        if (
            result["log_digest"] != provenance["setup_log_digest"]
            or result["dependency_artifact"] != provenance["artifact"]
        ):
            raise ValueError
        artifact = base64.b64decode(payload["dependency_archive"], validate=True)
        if artifact_manifest(artifact) != provenance["artifact"]:
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
        fixed_probe(payload["oom_operation"], worker, "memory")
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
        identity = record["execution_identity"]
        if (
            record["state"] != "exited"
            or result["exit_code"] is not None
            or result.get("artifact")
        ):
            raise ValueError
        if (
            identity["worker_boot"] != record["worker_boot"]
            or identity["instance_id"] != worker["instance_id"]
            or identity["runtime"] != "compact-worker-v1"
        ):
            raise ValueError
        if (
            result["execution_identity"] != identity
            or result["operation_id"] != record["manifest"]["operation_id"]
            or result["run_id"] != record["manifest"]["run_id"]
        ):
            raise ValueError
        if (
            result["terminated"] is not True
            or result["timed_out"] is not False
            or result["started_at"] is not None
        ):
            raise ValueError
        if (
            digest(payload["boundary"]) != worker["qualification_digest"]
            or payload["boundary"]["restart_receipt"] != record["receipt_digest"]
        ):
            raise ValueError
        fixed_probe(record, worker, "wait", reconciled=True)
    elif name == "runner_restart":
        before, after = payload["before"], payload["after"]
        if containers is None or before["Id"] != containers["runner"]:
            raise ValueError
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
        if after["State"]["Running"] is not True:
            raise ValueError
        if any(
            type(observed["RestartCount"]) is not int or observed["RestartCount"] < 0
            for observed in (before, after)
        ):
            raise ValueError(
                "Runner restart counters must be nonnegative integer observations."
            )
        for observed in (payload["controller_before"], payload["controller_after"]):
            if (
                observed["kind"] != "actual-controller-process"
                or observed["uid"] != 10001
            ):
                raise ValueError
            if any(
                type(observed[key]) is not int or observed[key] <= 0
                for key in ("pid", "start_ticks", "runner_id")
            ):
                raise ValueError
    elif name == "backup_restore":
        validate_backup(payload, worker)
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
            type(report["exit_code"]) is not int
            or report["exit_code"] != 0
            or report["native_receipt_version"] != 2
            or report["production_qualification"] is not False
        ):
            raise ValueError
        if not HEX.fullmatch(report["source_digest"]) or not payload["wheels"]:
            raise ValueError
        validate_wheels(payload)
        record = payload["operation"]
        identity = record["execution_identity"]
        protected_operation(record, identity)
        if record["manifest"]["source_digest"] != report["source_digest"]:
            raise ValueError
        if (
            record["manifest"]["profile_id"] != "offline-distributions"
            or record["manifest"]["phase"] != "setup"
        ):
            raise ValueError
        if (
            identity["runtime"] != "compose-slot-v2"
            or identity != report["execution_identity"]
        ):
            raise ValueError
    elif name == "browser":
        validate_browser(payload, source, worker)


def validate_backup(payload, worker):
    import base64
    from pathlib import PurePosixPath

    manifest = payload["manifest"]
    identity = manifest["backup_id"]
    if not isinstance(identity, str) or str(UUID(identity)) != identity:
        raise ValueError("Backup proof needs its exact canonical UUID.")
    artifacts = manifest["artifacts"]
    if set(artifacts) != {"database.dump", "state.tar", "inventory.json"}:
        raise ValueError("Backup artifacts are incomplete.")
    for artifact in artifacts.values():
        if set(artifact) != {"sha256", "bytes"} or not HEX.fullmatch(
            artifact["sha256"]
        ):
            raise ValueError("Backup artifact digest is invalid.")
        if (
            type(artifact["bytes"]) is not int
            or not 0 < artifact["bytes"] <= 64 * 1024**3
        ):
            raise ValueError("Backup artifact size is invalid.")
    files, database = payload["file_restore"], payload["database_restore"]
    for report in (files, database):
        if report["backup_id"] != identity or report["artifacts_verified"] != artifacts:
            raise ValueError(
                "Restore verification differs from the actual backup artifacts."
            )
    content = base64.b64decode(files["inventory_json"], validate=True)
    expected = artifacts["inventory.json"]
    if (
        len(content) != expected["bytes"]
        or hashlib.sha256(content).hexdigest() != expected["sha256"]
    ):
        raise ValueError("Inventory bytes differ from the verified artifact.")
    inventory = json.loads(content)
    if (
        not isinstance(inventory, list)
        or not 7 <= len(inventory) <= 250000
        or inventory != files["restored_inventory"]
    ):
        raise ValueError("Restored private-state inventory differs from the backup.")
    volumes, paths = set(manifest["state_volumes"]), set()
    directories, total = set(), 0
    for entry in inventory:
        path = PurePosixPath(entry["path"])
        if (
            path.is_absolute()
            or str(path) != entry["path"]
            or ".." in path.parts
            or path.parts[0] not in volumes
            or "\\" in entry["path"]
            or "\x00" in entry["path"]
            or len(entry["path"].encode()) > 4096
        ):
            raise ValueError("Private-state inventory contains an unsafe path.")
        if str(path) in paths or entry["kind"] not in {"file", "directory"}:
            raise ValueError("Private-state inventory entries are ambiguous.")
        paths.add(str(path))
        if any(
            type(entry[key]) is not int or entry[key] < 0
            for key in ("mode", "uid", "gid", "bytes")
        ):
            raise ValueError("Private-state ownership or size is invalid.")
        if entry["uid"] >= 2**32 or entry["gid"] >= 2**32:
            raise ValueError("Private-state inventory ownership is out of range.")
        allowed = 0o1777 if entry["kind"] == "directory" else 0o777
        if entry["mode"] & ~allowed:
            raise ValueError("Private-state inventory has unsupported file mode bits.")
        if entry["kind"] == "directory":
            if entry["bytes"] != 0 or entry["sha256"] is not None:
                raise ValueError("Directory inventory cannot carry file contents.")
            directories.add(str(path))
        total += entry["bytes"]
        if total > 64 * 1024**3:
            raise ValueError("Private-state inventory exceeds its byte bound.")
        if entry["kind"] == "file" and not HEX.fullmatch(entry["sha256"]):
            raise ValueError("Private-state file content digest is invalid.")
    if not volumes <= paths:
        raise ValueError("Private-state inventory omits a separate volume.")
    if not volumes <= directories:
        raise ValueError("Private-state volume roots must be directories.")
    for path in paths:
        parent = str(PurePosixPath(path).parent)
        if parent != "." and parent not in directories:
            raise ValueError(
                "Private-state inventory omits a declared parent directory."
            )
    tables = manifest["database"]["tables"]
    if database["restored_tables"] != tables or payload["restored_tables"] != tables:
        raise ValueError("Actual restored database fingerprints differ.")
    names = set()
    for table in tables:
        if (
            set(table) != {"schema", "table", "sha256"}
            or not table["schema"]
            or not table["table"]
            or not HEX.fullmatch(table["sha256"])
        ):
            raise ValueError("Database fingerprint table identity is incomplete.")
        names.add(table["table"])
    if (
        not {
            "credentials_credential",
            "projects_project",
            "work_queue_queueitem",
            "runner_control_run",
        }
        <= names
    ):
        raise ValueError("Database proof omits business or credential history tables.")
    fenced = {
        "runner_control_runneridentity",
        "runner_control_runnersettings",
        "runner_control_runnercontroller",
    }
    if database["business_tables_after_fencing"] != [
        table for table in tables if table["table"] not in fenced
    ]:
        raise ValueError("Business data changed while retiring restored authority.")
    restored, boundary = payload["restored_capabilities"], payload["restored_boundary"]
    if (
        restored["qualified"] is not True
        or restored["runtime"] != "compact-worker-v1"
        or restored["image_digest"] != worker["image_digest"]
    ):
        raise ValueError("Restored worker lacks current immutable-image qualification.")
    if (
        restored["instance_id"] == worker["instance_id"]
        or restored["worker_boot"] == worker["worker_boot"]
    ):
        raise ValueError("Restored worker reused retired authority.")
    if (
        payload["worker"]["retired_instance_id"] != worker["instance_id"]
        or payload["worker"]["retired_worker_boot"] != worker["worker_boot"]
    ):
        raise ValueError("Restored authority history differs from the original worker.")
    if (
        payload["worker"]["instance_id"] != restored["instance_id"]
        or payload["worker"]["image_digest"] != worker["image_digest"]
    ):
        raise ValueError("Retired-token checks differ from the restored worker.")
    for key in ("worker_boot", "binding_digest", "qualification_digest"):
        if payload["worker"][key] != restored[key]:
            raise ValueError(
                "Retired-token checks do not bind the qualified restored boot."
            )
    boundary_proof(boundary, restored)
    roles = restored["factory_roles"]
    if not set(ROLE_NAMES) <= set(roles) or not isinstance(
        restored["factory_browser"], dict
    ):
        raise ValueError("Restored worker lacks role and browser requalification.")
    entries = [roles[role] for role in ROLE_NAMES]
    entries.append(restored["factory_browser"])
    for entry in entries:
        proof = entry["qualification"]
        if proof["kind"] != "worker-boundary" or proof["passed"] is not True:
            raise ValueError(
                "Restored role proof did not pass its actual worker boundary."
            )
        for key in ("instance_id", "worker_boot", "image_digest"):
            if proof[key] != restored[key]:
                raise ValueError("Restored role proof belongs to retired authority.")
        if proof["runtime_binding_digest"] != restored["binding_digest"]:
            raise ValueError("Restored role proof has another runtime binding.")


def validate_wheels(payload):
    import base64
    import configparser
    import csv
    from email.parser import BytesParser
    import io
    from pathlib import PurePosixPath
    import zipfile

    artifact = base64.b64decode(payload["artifact_proof"], validate=True)
    expected = {
        "kind": "offline-packaging-artifacts",
        "wheels": payload["wheels"],
        "commands": payload["commands"],
    }
    if json.loads(artifact) != expected:
        raise ValueError(
            "Packaging artifact differs from the captured wheel and command evidence."
        )
    files = payload["operation"]["result"]["artifact"]["files"]
    proof_files = [entry for entry in files if entry["path"] == "packaging-proof.json"]
    if (
        len(proof_files) != 1
        or proof_files[0]["sha256"] != hashlib.sha256(artifact).hexdigest()
        or proof_files[0]["size"] != len(artifact)
    ):
        raise ValueError(
            "Packaging evidence differs from the protected source export inventory."
        )
    names = set()
    for wheel in payload["wheels"]:
        content = base64.b64decode(wheel["content"], validate=True)
        if (
            len(content) > 8 * 1024**2
            or hashlib.sha256(content).hexdigest() != wheel["sha256"]
        ):
            raise ValueError("Wheel bytes differ from their artifact digest.")
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                entries = archive.infolist()
                if (
                    len(entries) > 10000
                    or sum(entry.file_size for entry in entries) > 64 * 1024**2
                ):
                    raise ValueError("Wheel evidence exceeds its fixed bounds.")
                metadata = [
                    entry
                    for entry in entries
                    if entry.filename.endswith(".dist-info/METADATA")
                ]
                if len(metadata) != 1 or metadata[0].file_size > 1024**2:
                    raise ValueError("Wheel metadata is missing or ambiguous.")
                name = BytesParser().parsebytes(archive.read(metadata[0]))["Name"]
                if name not in {"vibearr-cli", "vibearr-runner", "vibearr-executor"}:
                    raise ValueError("Unexpected distribution artifact.")
                info = metadata[0].filename.rsplit("/", 1)[0]
                required = {
                    info + "/WHEEL",
                    info + "/RECORD",
                    info + "/entry_points.txt",
                }
                if not required <= set(archive.namelist()):
                    raise ValueError("Wheel installation metadata is incomplete.")
                wheel_metadata = BytesParser().parsebytes(archive.read(info + "/WHEEL"))
                if wheel_metadata["Wheel-Version"] != "1.0":
                    raise ValueError("Wheel format is unsupported.")
                configuration = configparser.ConfigParser()
                configuration.read_string(
                    archive.read(info + "/entry_points.txt").decode()
                )
                commands = set(configuration["console_scripts"])
                expected = {
                    "vibearr-cli": {"vibearr", "nq"},
                    "vibearr-runner": {"vibearr-runner", "coding-runner"},
                    "vibearr-executor": {"vibearr-executor"},
                }
                if not expected[name] <= commands:
                    raise ValueError("Wheel console entrypoints are incomplete.")
                records = csv.reader(
                    io.StringIO(archive.read(info + "/RECORD").decode())
                )
                checked = set()
                for filename, checksum, size in records:
                    checked.add(filename)
                    if filename == info + "/RECORD":
                        continue
                    member = archive.read(filename)
                    sha = (
                        base64.urlsafe_b64encode(hashlib.sha256(member).digest())
                        .rstrip(b"=")
                        .decode()
                    )
                    if checksum != "sha256=" + sha or int(size) != len(member):
                        raise ValueError("Wheel RECORD differs from its actual files.")
                if checked != set(archive.namelist()):
                    raise ValueError("Wheel RECORD omits distribution files.")
                names.add(name)
        except zipfile.BadZipFile as error:
            raise ValueError(
                "Wheel artifact is not a valid ZIP distribution."
            ) from error
    if names != {"vibearr-cli", "vibearr-runner", "vibearr-executor"}:
        raise ValueError("Packaging evidence omits a required distribution.")
    commands = payload["commands"]
    help_commands = set()
    installed = False
    for command in commands:
        arguments = command["argv"]
        if (
            type(command["exit_code"]) is not int
            or command["exit_code"] != 0
            or not HEX.fullmatch(command["stdout_sha256"])
        ):
            raise ValueError("An artifact execution check failed.")
        if arguments[-1:] == ["--help"]:
            help_commands.add(PurePosixPath(arguments[0]).name)
        installed = installed or "install" in arguments and "--no-index" in arguments
    if (
        not installed
        or not {"vibearr", "nq", "vibearr-runner", "coding-runner"} <= help_commands
    ):
        raise ValueError("Installed CLI command execution evidence is incomplete.")


def validate_browser(payload, source, worker):
    import base64
    import tempfile
    from pathlib import Path
    from vibearr_executor.artifacts import unpack
    from vibearr_executor.contracts import Limits, Manifest
    from vibearr_executor.local_services import browser_profile
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
    identity = {
        "runtime": "compact-worker-v1",
        "protocol_version": 2,
        **{
            key: worker[key] for key in ("binding_digest", "instance_id", "worker_boot")
        },
    }
    if observed["execution_identity"] != identity or {
        binding["application_image"],
        binding["browser_image"],
    } != {worker["image_digest"]}:
        raise ValueError
    manifest, result = protected_operation(payload["operation"], worker)
    expected = browser_profile(worker["image_digest"], Limits(2, 4096, 128, 2048))
    Manifest.parse_archived(manifest, expected, result["started_at"])
    if (
        manifest["source_digest"] != binding["source_digest"]
        or observed["operation_id"] != manifest["operation_id"]
        or observed["run_id"] != manifest["run_id"]
        or observed["profile_digest"] != expected.digest
    ):
        raise ValueError
    if manifest["context_digest"] != digest({"browser_binding": binding}):
        raise ValueError("Browser binding differs from its protected context.")
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        for path, (_mode, content) in unpack(archive).items():
            target = root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        validate_worker_report(
            report, binding=binding, worker_binding=observed, storage_root=root
        )


def validate_observations(value, containers):
    import math

    try:
        observations = value["runtime_observations"]
        phases = observations["phases"]
        expected_phases = {
            "startup",
            "boundary",
            "dependency",
            "candidate",
            "browser",
            "beforebackup",
        }
        if (
            len(phases) != len(expected_phases)
            or {entry["phase"] for entry in phases} != expected_phases
        ):
            raise ValueError("Sequential runtime phase observations are incomplete.")
        expected_ids = set(containers.values())
        for entry in phases:
            if (
                len(entry["containers"]) != 4
                or set(entry["containers"]) != expected_ids
            ):
                raise ValueError(
                    "Normal task phases changed the four actual runtime containers."
                )
        idle = observations["idle"]
        if len(idle) != 4 or {entry["ID"] for entry in idle} != expected_ids:
            raise ValueError(
                "Idle resource observations omit an actual runtime container."
            )
        for entry in idle:
            for key in ("CPUPerc", "MemPerc"):
                if not isinstance(entry[key], str) or not entry[key].endswith("%"):
                    raise ValueError("Runtime resource percentage is missing.")
                percentage = float(entry[key][:-1])
                if (
                    not math.isfinite(percentage)
                    or percentage < 0
                    or key == "MemPerc"
                    and percentage > 100
                ):
                    raise ValueError("Runtime resource observation is out of range.")
            if (
                not isinstance(entry["PIDs"], str)
                or not entry["PIDs"].isdigit()
                or not 0 < int(entry["PIDs"]) <= 128
            ):
                raise ValueError(
                    "Idle PID observation is missing or exceeds its actual ceiling."
                )
            if not isinstance(entry["MemUsage"], str) or " / " not in entry["MemUsage"]:
                raise ValueError("Idle memory observation is missing.")
        resources = observations["worker_peak"]
        if (
            resources != value["worker_boundary"]["resources"]
            or int(resources["memory_peak"]) <= 0
            or int(resources["pids_peak"]) <= 0
        ):
            raise ValueError(
                "Peak resource observations differ from the protected boundary proof."
            )
    except (KeyError, TypeError, ValueError, AttributeError) as error:
        raise CompactEvidenceError(
            "Actual sequential phase or resource observations are invalid."
        ) from error


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
    if not HEX.fullmatch(value.get("source_digest", "")) or not re.fullmatch(
        r"[a-f0-9]{40}|[a-f0-9]{64}", value.get("tree_sha", "")
    ):
        raise CompactEvidenceError(
            "Engineering evidence lacks an exact candidate archive or tree."
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
    validate_observations(value, containers)
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
    try:
        boundary_proof(boundary, worker)
    except (KeyError, TypeError, ValueError, AttributeError) as error:
        raise CompactEvidenceError(
            "Worker boundary lacks complete protected execution proof."
        ) from error
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
            if any(
                receipt["document"].get(key) != value[key]
                for key in ("source_digest", "tree_sha")
            ):
                raise CompactEvidenceError(
                    "Engineering document differs from the exact frozen candidate."
                )
            validate_document(
                name, receipt["document"], source, worker, images, containers
            )
    return value
