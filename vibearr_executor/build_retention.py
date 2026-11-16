"""Purge only terminal build payloads and their exact owned image."""

from __future__ import annotations

from datetime import datetime, timezone
import shutil
from typing import TYPE_CHECKING

from .contracts import Conflict, Profile, identifier
from .engine import EngineError
from . import retention

if TYPE_CHECKING:
    from .dependency_builds import DependencyBuilds


def proof_states(builds: DependencyBuilds, build_id: str):
    root = builds.path(build_id) / "qualification"
    if root.exists():
        yield root
        yield from root.glob("image-probe-*")


def image_referenced(builds: DependencyBuilds, build_id: str, image: str) -> bool:
    runtime = builds.runtime
    if any(
        image in {profile.image, profile.database_image}
        for profile in runtime.profiles.values()
    ):
        return True
    for path in runtime.operations.iterdir():
        record = runtime.record(path.name)
        if record.get("profile", {}).get(
            "image"
        ) == image and not retention.fully_purged(record):
            return True
    for path in builds.directory.iterdir():
        if path.name == build_id:
            continue
        record = builds.read(path.name)
        if retention.fully_purged(record):
            continue
        if image in {
            record.get("image"),
            record["request"]["validation_profile"]["image"],
        }:
            return True
    return False


def remove_image(builds: DependencyBuilds, record: dict) -> None:
    if not record.get("image_commit_intent"):
        return
    build_id = record["request"]["build_id"]
    image = builds.committed_image(record)
    if image is None:
        if not record.get("image_remove_intent"):
            raise Conflict(
                "The owned committed image is missing; reconcile before retention."
            )
        return
    if record.get("image") and image != record["image"]:
        raise Conflict("The retained build image identity changed.")
    if image_referenced(builds, build_id, image):
        raise Conflict(
            "The derived image remains referenced by retained operations or operator profiles."
        )
    record["image_remove_intent"] = image
    builds.save(record)
    builds.runtime.docker.run("image", "rm", image)
    if builds.committed_image(record) is not None:
        raise EngineError("Derived image removal was not confirmed.")


def purge_children(
    builds: DependencyBuilds, build_id: str, requested: set[str]
) -> None:
    import json
    from .runtime import Runtime

    for state in proof_states(builds, build_id):
        profiles_path = state / "profiles.json"
        if not profiles_path.exists():
            continue
        profiles = [
            Profile.parse(value) for value in json.loads(profiles_path.read_bytes())
        ]
        child = Runtime(
            state,
            profiles,
            docker=builds.runtime.docker,
            denied_ips=builds.runtime.configured_ips,
        )
        try:
            for path in child.operations.iterdir():
                record = child.record(path.name)
                child.purge(path.name, record["manifest"]["run_id"], sorted(requested))
        finally:
            child.close()
        if "source" in requested:
            (state / "token").unlink(missing_ok=True)
        if "logs" in requested:
            (state / "server.log").unlink(missing_ok=True)


def purge(builds: DependencyBuilds, build_id: str, run_id: str, selected) -> dict:
    identifier(run_id)
    requested = retention.resources(selected)
    with builds.runtime.lock:
        record = builds.read(build_id)
        if record["request"]["run_id"] != run_id:
            raise Conflict("Retention run does not own this dependency build.")
        if record["state"] not in {"ready", "failed", "canceled", "purged"}:
            raise Conflict("Active or unknown dependency builds cannot be purged.")
        thread = builds.threads.get(build_id)
        if thread is not None and thread.is_alive():
            raise Conflict("The dependency-build worker has not terminated.")
        if any(builds.processes(state) for state in proof_states(builds, build_id)):
            raise Conflict(
                "Qualification still owns processes; retain the build fence."
            )
        setup = builds.runtime.record(build_id)
        if (
            setup.get("state") != "exited"
            or setup.get("descendants_terminated") is not True
        ):
            raise Conflict(
                "Confirm setup termination before purging dependency artifacts."
            )
        done = set(record.get("resources_removed", []))
        pending = (requested | set(record.get("purge_intent", []))) - done
        if not pending:
            return builds.public(record)
        record["purge_intent"] = sorted(pending)
        builds.save(record)
        builds.cleanup_qualification(record)
        if record.get("assembly_intent"):
            builds.cleanup_assembly(record)
        if "source" in pending:
            remove_image(builds, record)
        purge_children(builds, build_id, pending)
        builds.runtime.purge(build_id, run_id, sorted(pending))
        root = builds.path(build_id)
        if "source" in pending:
            if (root / "layer").exists():
                shutil.rmtree(root / "layer")
            (root / "requirements.txt").unlink(missing_ok=True)
            (root / "profile.json").unlink(missing_ok=True)
        if "logs" in pending:
            (root / "qualification.log").unlink(missing_ok=True)
        if "source" in pending:
            content_digest = record["request"]["source_digest"]
            if not retention.artifact_referenced(builds.runtime, content_digest):
                (builds.runtime.artifacts / content_digest).unlink(missing_ok=True)
        done.update(pending)
        record["resources_removed"] = sorted(done)
        record["purge_intent"] = []
        record["tombstone"] = done == retention.RESOURCES
        if record["tombstone"]:
            record["state"] = "purged"
        record.setdefault("removed_at", {}).update(
            {key: datetime.now(timezone.utc).isoformat() for key in pending}
        )
        builds.save(record)
        return builds.public(record)
