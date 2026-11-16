"""Actual OCI control and terminal-retention qualification, without model calls."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import secrets
import time
from typing import TYPE_CHECKING

from .artifacts import pack, unpack
from .client import ExecutorArtifactRemoved, ExecutorRejected
from .contracts import Limits, Profile, digest
from .engine import Docker, EngineError

if TYPE_CHECKING:
    from .qualify import Harness

COUNTER = """import os,pathlib,time
end=time.monotonic()+600
counter=0
while time.monotonic()<end:
    counter+=1
    pathlib.Path('pause-count.next').write_text(str(counter))
    os.replace('pause-count.next','pause-count.txt')
    time.sleep(.05)
"""


def control_profile(image: str) -> Profile:
    return Profile(
        "smoke-controls-v1",
        1,
        image,
        "validation",
        ("/usr/local/bin/python3.12", "-I", "-c", COUNTER),
        Limits(),
    )


def require(value, message):
    if not value:
        raise RuntimeError(message)


def counter(docker: Docker, container: str) -> int:
    value = docker.run(
        "exec", "--user", "0:0", container, "cat", "/work/pause-count.txt", limit=32
    )
    require(len(value) <= 32, "Control counter exceeds its bound")
    return int(value)


def wait_counter(docker: Docker, container: str, after: int = -1) -> int:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            value = counter(docker, container)
            if value > after:
                return value
        except EngineError:
            pass
        time.sleep(0.1)
    raise RuntimeError("Actual control task did not advance its counter")


def short_manifest(harness: Harness, kind: str, source: bytes, seconds: int) -> dict:
    from uuid import uuid4
    from dataclasses import asdict

    profile = next(
        profile
        for profile in harness.profiles
        if profile.profile_id == f"smoke-{kind}-v1"
    )
    context = b"Fixed control/deadline qualification."
    harness.client.upload("source", source)
    harness.client.upload("context", context)
    manifest = {
        "schema_version": 1,
        "run_id": str(uuid4()),
        "operation_id": str(uuid4()),
        "profile_id": profile.profile_id,
        "profile_digest": profile.digest,
        "source_digest": digest(source),
        "context_digest": digest(context),
        "phase": profile.phase,
        "limits": asdict(profile.limits),
        "network_hosts": list(profile.network_hosts),
        "deadline": (
            datetime.now(timezone.utc) + timedelta(seconds=seconds)
        ).isoformat(),
    }
    harness.client.prepare(manifest)
    return manifest


def pause_resume_interval(
    harness: Harness, docker: Docker, operation: str, container: str, original_pid: int
) -> dict:
    from .qualification_counter import CounterObserver

    paused = harness.client.pause(operation)
    require(
        paused["paused"] and paused["state"] == "running", "Actual task did not pause"
    )
    observer = CounterObserver(harness.state, operation, docker)
    baseline = observer.read()
    time.sleep(1)
    harness.shutdown()
    harness.start()
    recovered = harness.client.pause(operation)
    require(
        recovered["container_id"] == container and recovered["paused"],
        "Pause restart lost original identity",
    )
    still_paused = observer.read()
    require(
        baseline["counter"] == still_paused["counter"],
        f"Frozen counter changed: before={baseline['counter']} after={still_paused['counter']} container={container}",
    )
    resumed = harness.client.resume(operation)
    require(
        resumed["container_id"] == container and not resumed["paused"],
        "Resume replaced or failed to unfreeze the invocation",
    )
    advanced = wait_counter(docker, container, baseline["counter"])
    inspected = docker.inspect(container)
    require(inspected is not None, "Resumed control container is missing")
    assert inspected is not None
    require(
        inspected["State"]["Pid"] == original_pid,
        "Resume replaced the original task PID",
    )
    return {
        "frozen_counter": baseline["counter"],
        "recovered_counter": still_paused["counter"],
        "resumed_counter": advanced,
        "observer_reads": [baseline, still_paused],
    }


def qualify_controls(harness: Harness, source: bytes, operations: list[str]) -> dict:
    docker = Docker()
    manifest = harness.prepare("controls", source)
    operation = manifest["operation_id"]
    operations.append(operation)
    started = harness.client.start(operation)
    container = started["container_id"]
    wait_counter(docker, container)
    inspected = docker.inspect(container)
    require(inspected is not None, "Original control container is missing")
    assert inspected is not None
    original_pid = inspected["State"]["Pid"]
    try:
        harness.client.purge(operation, run_id=manifest["run_id"])
    except ExecutorRejected as exc:
        require(exc.http_status == 409, "Running purge returned an unexpected outcome")
    else:
        raise RuntimeError("Running operation was purged")
    pause_proof = pause_resume_interval(
        harness, docker, operation, container, original_pid
    )
    harness.client.pause(operation)
    stopped = harness.client.stop(operation)
    require(
        stopped["state"] == "exited" and stopped["descendants_terminated"],
        "Paused cancellation was not confirmed",
    )
    require(docker.inspect(container) is None, "Canceled paused container survived")

    expiring = short_manifest(harness, "controls", source, 10)
    expiring_id = expiring["operation_id"]
    operations.append(expiring_id)
    original = harness.client.start(expiring_id)
    wait_counter(docker, original["container_id"])
    harness.client.pause(expiring_id)
    harness.shutdown()
    remaining = datetime.fromisoformat(expiring["deadline"]).timestamp() - time.time()
    time.sleep(max(0, remaining) + 0.15)
    harness.start()
    expired = harness.finished(expiring_id, timeout=30)
    require(
        expired["result"].get("timed_out") is True,
        "Paused original deadline was extended",
    )
    require(
        expired["descendants_terminated"]
        and docker.inspect(original["container_id"]) is None,
        "Expired paused container remains alive",
    )
    require(
        harness.client.resume(expiring_id) == expired,
        "Expired operation was resumed or replaced",
    )

    unlaunched = short_manifest(harness, "agent", source, 2)
    unlaunched_id = unlaunched["operation_id"]
    operations.append(unlaunched_id)
    harness.client.supply_secret(
        unlaunched_id, "qualification-unlaunched-" + secrets.token_hex(16)
    )
    time.sleep(2.2)
    erased = harness.finished(unlaunched_id, timeout=10)
    require(
        erased["result"].get("termination") == "expired-before-start",
        "Never-started secret did not expire",
    )
    require(
        "container_id" not in erased
        and not (
            harness.state / "operations" / unlaunched_id / "input/secret"
        ).read_bytes(),
        "Unlaunched secret survived or a container started",
    )

    unique = unpack(source)
    unique["retention-marker.txt"] = (0o644, secrets.token_hex(16).encode())
    retained_source = pack(unique)
    retained = harness.prepare("validation", retained_source)
    retained_id = retained["operation_id"]
    operations.append(retained_id)
    second = harness.prepare("validation", retained_source)
    second_id = second["operation_id"]
    operations.append(second_id)
    harness.client.start(retained_id)
    completed = harness.finished(retained_id)
    require(completed["result"]["exit_code"] == 0, "Retention source task failed")
    logs = harness.client.logs(retained_id)
    harness.client.purge(retained_id, run_id=retained["run_id"], resources=("source",))
    require(
        harness.client.logs(retained_id) == logs, "Source purge removed retained logs"
    )
    require(
        (harness.state / "artifacts" / digest(retained_source)).exists(),
        "A shared source reference was deleted",
    )
    tombstone = harness.client.purge(
        retained_id, run_id=retained["run_id"], resources=("logs",)
    )
    require(tombstone["tombstone"] is True, "Terminal purge did not retain a tombstone")
    for action in (
        lambda: harness.client.logs(retained_id),
        lambda: harness.client.export(retained_id, retained_source),
    ):
        try:
            action()
        except ExecutorArtifactRemoved:
            pass
        else:
            raise RuntimeError("Purged payload did not report known unavailability")
    require(
        harness.client.prepare(retained) == tombstone
        and harness.client.start(retained_id) == tombstone,
        "Purged identity could launch again",
    )
    harness.client.stop(second_id)
    harness.client.purge(second_id, run_id=second["run_id"])
    require(
        not (harness.state / "artifacts" / digest(retained_source)).exists(),
        "Unreferenced source artifact survived full purge",
    )
    return {
        "pause": {
            "operation_id": operation,
            "container_id": container,
            "pid": original_pid,
            **pause_proof,
            "deadline": manifest["deadline"],
        },
        "paused_expiry": expired,
        "unlaunched_expiry": erased,
        "retention": tombstone,
    }
