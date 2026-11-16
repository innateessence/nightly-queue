"""Build pause intent is independent of an in-flight fixed assembly step."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from .contracts import Conflict, canonical, digest
from .engine import EngineError
from .operation_controls import expired

if TYPE_CHECKING:
    from .dependency_builds import DependencyBuilds

TERMINAL = {"ready", "failed", "canceled", "unknown", "purged"}


def control(builds: DependencyBuilds, build_id: str) -> dict:
    path = builds.path(build_id) / "control.json"
    if not path.exists():
        return {"generation": 0, "pause_requested": False, "resume_authorized": True}
    value = json.loads(path.read_bytes())
    if (
        set(value) != {"generation", "pause_requested", "resume_authorized"}
        or type(value["generation"]) is not int
        or value["generation"] < 0
        or type(value["pause_requested"]) is not bool
        or type(value["resume_authorized"]) is not bool
    ):
        raise EngineError("Private build control is invalid; preserve its fence.")
    return value


def save_control(builds: DependencyBuilds, build_id: str, value: dict) -> None:
    from .runtime import durable

    durable(builds.path(build_id) / "control.json", canonical(value))


def gated(builds: DependencyBuilds, build_id: str) -> bool:
    value = control(builds, build_id)
    return value["pause_requested"] or not value["resume_authorized"]


def public(builds: DependencyBuilds, record: dict) -> dict:
    build_id = record["request"]["build_id"]
    value = control(builds, build_id)
    if not gated(builds, build_id):
        return {"pause_requested": False, "paused": False, "pause_state": "running"}
    state = record["state"]
    operation = builds.runtime.record(build_id)
    settled = state in {"ready", "failed", "canceled", "purged"}
    settled = settled or state == "prepared" and not operation.get("launch_intent")
    settled = (
        settled
        or state == "setup"
        and (operation.get("paused") is True or operation.get("state") == "exited")
    )
    settled = settled or state == "assembling" and not record.get("assembly_intent")
    settled = (
        settled or state == "qualifying" and not record.get("qualification_intent")
    )
    phase = "paused" if settled else "pausing-at-checkpoint"
    if not value["pause_requested"]:
        phase = "resuming"
    return {
        "pause_requested": value["pause_requested"],
        "paused": bool(settled),
        "pause_state": phase,
    }


def checkpoint(builds: DependencyBuilds, record: dict) -> bool:
    """Return true only while an existing qualification still needs observation."""
    build_id = record["request"]["build_id"]
    if record["state"] in {"prepared", "setup"}:
        builds.runtime.pause(build_id)
        return False
    if record["state"] == "qualifying" and record.get("qualification_intent"):
        builds.step(record)
        return record["state"] == "qualifying"
    return False


def pause(builds: DependencyBuilds, build_id: str) -> dict:
    record = builds.read(build_id)
    if record["state"] == "purged":
        return builds.public(record)
    with builds.control_lock:
        value = control(builds, build_id)
        if not value["pause_requested"]:
            value.update(
                generation=value["generation"] + 1,
                pause_requested=True,
                resume_authorized=False,
            )
            save_control(builds, build_id, value)
    if builds.runtime.lock.acquire(blocking=False):
        try:
            record = builds.read(build_id)
            if record["state"] not in TERMINAL:
                checkpoint(builds, record)
        finally:
            builds.runtime.lock.release()
    return builds.public(builds.read(build_id))


def resume(builds: DependencyBuilds, build_id: str) -> dict:
    from .qualification import binding, capabilities

    record = builds.read(build_id)
    if record["state"] == "purged":
        return builds.public(record)
    with builds.control_lock:
        value = control(builds, build_id)
        if not value["pause_requested"] and value["resume_authorized"]:
            return builds.public(record)
        value.update(
            generation=value["generation"] + 1,
            pause_requested=False,
            resume_authorized=False,
        )
        generation = value["generation"]
        save_control(builds, build_id, value)
    with builds.runtime.lock:
        record = builds.read(build_id)
        if record["state"] in {"failed", "canceled", "unknown", "purged"}:
            return builds.public(record)
        if expired(builds.runtime.record(build_id)):
            return expire(builds, record)
        if digest(canonical(binding(builds.runtime))) != record["parent_binding"]:
            raise Conflict(
                "Paused build parent binding changed; stop and reconcile it."
            )
        if not capabilities(builds.runtime)["qualified"]:
            raise Conflict(
                "Current runtime qualification does not permit build resume."
            )
        with builds.control_lock:
            current = control(builds, build_id)
            if current["generation"] != generation:
                return builds.public(record)
            builds.runtime.resume(build_id)
            current = control(builds, build_id)
            if current["generation"] != generation:
                checkpoint(builds, record)
                return builds.public(record)
            current["resume_authorized"] = True
            save_control(builds, build_id, current)
        return builds.start(build_id)


def expire(builds: DependencyBuilds, record: dict) -> dict:
    build_id = record["request"]["build_id"]
    builds.stop(build_id)
    record = builds.read(build_id)
    record["deadline_expired"] = True
    record["wait_reason"] = "Original dependency-build deadline expired while paused."
    builds.save(record)
    return builds.public(record)


def enforce(builds: DependencyBuilds, build_id: str) -> None:
    record = builds.read(build_id)
    if record["state"] in TERMINAL or not gated(builds, build_id):
        return
    if not builds.runtime.lock.acquire(blocking=False):
        return
    try:
        record = builds.read(build_id)
        if expired(builds.runtime.record(build_id)):
            expire(builds, record)
        elif checkpoint(builds, record):
            builds.start(build_id, observe_paused=True)
    finally:
        builds.runtime.lock.release()
