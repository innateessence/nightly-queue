"""Dependency evidence binds an artifact, not a dynamically created image."""

from .contracts import Rejected, identifier, sha256
from .dependency_artifacts import MAX_DEPENDENCIES, MAX_DEPENDENCY_FILES


def validate(value, profile, *, parent=None, run_id=None):
    expected = {
        "schema_version",
        "build_id",
        "run_id",
        "source_digest",
        "requirements_digest",
        "dependency_digest",
        "parent_image",
        "parent_profile_digest",
        "parent_runtime_binding",
        "setup_operation_id",
        "setup_log_digest",
        "artifact",
        "validation_profile_digest",
        "setup_instance_id",
        "setup_worker_boot",
    }
    if (
        set(value) != expected
        or type(value["schema_version"]) is not int
        or value["schema_version"] != 2
    ):
        raise Rejected("Supply the complete socketless dependency provenance.")
    for key in (
        "build_id",
        "run_id",
        "setup_operation_id",
        "setup_instance_id",
        "setup_worker_boot",
    ):
        identifier(value[key])
    for key in (
        "source_digest",
        "requirements_digest",
        "dependency_digest",
        "parent_profile_digest",
        "parent_runtime_binding",
        "setup_log_digest",
        "validation_profile_digest",
    ):
        sha256(value[key])
    artifact = value["artifact"]
    if not isinstance(artifact, dict) or set(artifact) != {
        "schema_version",
        "sha256",
        "bytes",
        "files",
        "requirements_digest",
    }:
        raise Rejected("Dependency artifact metadata is incomplete.")
    if (
        type(artifact["schema_version"]) is not int
        or artifact["schema_version"] != 1
        or type(artifact["bytes"]) is not int
        or not 1 <= artifact["bytes"] <= MAX_DEPENDENCIES
        or type(artifact["files"]) is not int
        or not 1 <= artifact["files"] <= MAX_DEPENDENCY_FILES
    ):
        raise Rejected("Dependency artifact metadata exceeds its fixed bounds.")
    if (
        value["parent_image"] != profile.image
        or value["validation_profile_digest"] != profile.digest
        or artifact["sha256"] != value["dependency_digest"]
        or artifact["requirements_digest"] != value["requirements_digest"]
        or value["requirements_digest"] != profile.dependency_digest
        or value["setup_operation_id"] != value["build_id"]
    ):
        raise Rejected(
            "Dependency artifact differs from the frozen run or validation recipe."
        )
    if parent is not None and (
        parent.image != profile.image or parent.digest != value["parent_profile_digest"]
    ):
        raise Rejected(
            "Dependency artifact parent differs from the configured immutable image."
        )
    if run_id is not None and value["run_id"] != run_id:
        raise Rejected("Dependency artifacts cannot cross run ownership.")
    return value
