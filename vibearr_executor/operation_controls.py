"""Fixed operation controls; none can create a replacement invocation."""

from __future__ import annotations

from datetime import datetime
import time
from pathlib import Path
from typing import TYPE_CHECKING

from .contracts import Conflict, Rejected, canonical, digest
from .engine import EngineError

if TYPE_CHECKING:
    from .runtime import Runtime


def deadline_record(manifest: dict) -> dict:
    remaining = datetime.fromisoformat(manifest["deadline"]).timestamp() - time.time()
    return {
        "deadline_boot": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
        "deadline_monotonic": time.monotonic() + max(0, remaining),
    }


def expired(record: dict) -> bool:
    if (
        time.time()
        >= datetime.fromisoformat(record["manifest"]["deadline"]).timestamp()
    ):
        return True
    boot = record.get("deadline_boot")
    if boot is None:
        return False
    if boot != Path("/proc/sys/kernel/random/boot_id").read_text().strip():
        return True
    return time.monotonic() >= record["deadline_monotonic"]


def is_probe(record: dict) -> bool:
    from .qualify import smoke_profiles

    profile = record["profile"]
    return any(
        candidate.data() == profile
        for candidate in smoke_profiles(profile["image"], profile["database_image"])
    )


def launch_binding(runtime: Runtime, record: dict) -> None:
    from .qualification import binding

    if not record.get("launch_binding_digest"):
        record["launch_binding_digest"] = digest(canonical(binding(runtime)))


def forced_stop(runtime: Runtime, record: dict, *, timed_out: bool = False) -> dict:
    from .runtime import durable

    record["stop_intent"] = True
    if not record.get("result"):
        record["result"] = {
            "schema_version": 1,
            "run_id": record["manifest"]["run_id"],
            "operation_id": record["operation_id"],
            "termination": "paused-deadline" if timed_out else "forced-stop",
            "exit_code": None,
            "terminated": True,
            "timed_out": timed_out,
            "canceled": not timed_out,
            "ended_at": time.time(),
            "export_error": "Forced termination; no verified workspace export.",
        }
        if timed_out and not record.get("launch_intent"):
            record["result"]["termination"] = "expired-before-start"
        durable(
            runtime.path(record["operation_id"]) / "result.json",
            canonical(record["result"]),
        )
    secret = runtime.path(record["operation_id"]) / "input" / "secret"
    if secret.parent.exists():
        durable(secret, b"")
    runtime.save(record)
    if record.get("launch_intent"):
        runtime.cleanup(record)
    else:
        record.update(
            state="exited",
            descendants_terminated=True,
            paused=False,
            pause_intent=False,
        )
        runtime.save(record)
    return runtime.public(record)


def observe_pause(runtime: Runtime, record: dict, inspected: dict) -> bool:
    if record.get("pause_intent") and not inspected["State"].get("Paused"):
        runtime.docker.run("pause", inspected["Id"])
        observed = runtime.owned(record, record["name"])
        if observed is None or not observed["State"].get("Paused"):
            raise EngineError(
                "Pause outcome is uncertain; retain the original invocation."
            )
        inspected = observed
    paused = inspected["State"].get("Paused") is True
    if paused:
        record["paused"] = True
        runtime.save(record)
        if expired(record):
            forced_stop(runtime, record, timed_out=True)
        return True
    if record.get("paused"):
        record["paused"] = False
        runtime.save(record)
    return False


def pause(runtime: Runtime, operation_id: str) -> dict:
    with runtime.lock:
        record = runtime.record(operation_id)
        if "manifest" not in record:
            raise Rejected("Prepare the operation before pausing it.")
        if record["state"] == "exited":
            return runtime.public(record)
        if record["state"] == "unknown":
            raise Conflict(
                "Unknown execution cannot be paused or resumed; reconcile or stop it."
            )
        record["pause_intent"] = True
        runtime.save(record)
        return runtime.status(operation_id)


def resume(runtime: Runtime, operation_id: str) -> dict:
    from .qualification import binding, capabilities

    with runtime.lock:
        observed = runtime.status(operation_id)
        record = runtime.record(operation_id)
        if "manifest" not in record:
            raise Rejected("Prepare the operation before resuming it.")
        if observed["state"] in {"not-started", "exited"}:
            if observed["state"] == "not-started":
                record["pause_intent"] = False
                runtime.save(record)
            return runtime.public(record)
        if observed["state"] == "unknown":
            raise Conflict("Unknown execution cannot be resumed; reconcile or stop it.")
        if not observed.get("paused") and not record.get("pause_intent"):
            return observed
        if expired(record):
            return forced_stop(runtime, record, timed_out=True)
        if not runtime.profile_is_current(record):
            raise Rejected(
                "Paused profile is no longer authorized; stop the original operation."
            )
        current = digest(canonical(binding(runtime)))
        if current != record.get("launch_binding_digest"):
            raise Rejected(
                "Paused runtime binding changed; stop the original operation."
            )
        if not is_probe(record) and not capabilities(runtime)["qualified"]:
            raise Rejected("Current runtime qualification does not permit resume.")
        inspected = runtime.owned(record, record["name"])
        if inspected is None or not inspected["State"]["Running"]:
            raise Conflict(
                "The original paused container is unavailable; retain its fence."
            )
        record["pause_intent"] = False
        runtime.save(record)
        if inspected["State"].get("Paused"):
            runtime.docker.run("unpause", inspected["Id"])
        return runtime.status(operation_id)
