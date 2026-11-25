"""Current worker restart witnesses and fixed qualification operations."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import time
from uuid import uuid4

from .artifacts import pack
from .contracts import Conflict, Manifest, Rejected, canonical, digest
from .runtime import durable

ARGV = ("/usr/local/bin/python", "-I", "-m", "vibearr_executor.slot_probe")


def wait(runtime, operation, *, terminal=True):
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        value = runtime.status(operation)
        if terminal and value["state"] in {"exited", "unknown"}:
            return value
        if not terminal and runtime.logs(operation, 0, 65536):
            return value
        time.sleep(0.05)
    raise Rejected("Qualification operation did not reach its bounded checkpoint.")


def prepare(runtime, case, *, seconds=30):
    base = next(iter(runtime.profiles.values()))
    profile = replace(
        base,
        profile_id="worker-qualification",
        argv=ARGV,
        backend_secret_env="",
    )
    operation = str(uuid4())
    runtime.probe_operation, runtime.probe_profile = operation, profile
    runtime.qualifying = True
    source = pack({"payload.txt": (0o644, b"fresh candidate\n")})
    context = canonical({"case": case})
    for kind, content in (("source", source), ("context", context)):
        runtime.upload(kind, content, digest(content))
    manifest = Manifest(
        1,
        str(uuid4()),
        operation,
        profile.profile_id,
        profile.digest,
        digest(source),
        digest(context),
        profile.phase,
        profile.limits,
        (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(),
        profile.network_hosts,
    )
    first = runtime.prepare(manifest.data())
    if first != runtime.prepare(manifest.data()):
        raise Rejected("Repeated prepare changed the qualification operation.")
    return operation


def restart_witness(runtime):
    path = runtime.directory / "restart-witness.json"
    if not path.exists():
        raise Conflict("Active work is not the qualification restart witness.")
    value = json.loads(path.read_bytes())
    if value["operation_id"] != runtime.active or value["worker_boot"] != runtime.boot:
        raise Conflict("Active work is not the current restart witness.")
    return value


def prepare_restart(runtime):
    identity = prepare(runtime, "wait", seconds=180)
    value = {
        "operation_id": identity,
        "worker_boot": runtime.boot,
        "instance_id": runtime.instance,
    }
    durable(runtime.directory / "restart-witness.json", canonical(value))
    runtime.start(identity)
    wait(runtime, identity, terminal=False)
    return value


def check_restart(runtime):
    path = runtime.directory / "restart-witness.json"
    if not path.exists():
        raise Rejected(
            "Prepare a witness and restart this Docker worker before qualification."
        )
    value = json.loads(path.read_bytes())
    record = runtime.record(value["operation_id"])
    if value["instance_id"] != runtime.instance or value["worker_boot"] == runtime.boot:
        raise Rejected(
            "Qualification requires the same worker volume and a new worker boot."
        )
    if (
        record["state"] != "exited"
        or record.get("descendants_terminated") is not True
        or record.get("result", {}).get("worker_restart") is not True
    ):
        raise Rejected(
            "The interrupted witness was not reconciled as terminated without re-execution."
        )
    if record["result"].get("exit_code") is not None or record["result"].get(
        "artifact"
    ):
        raise Rejected("Restart reconciliation fabricated an execution result.")
    return record["receipt_digest"]
