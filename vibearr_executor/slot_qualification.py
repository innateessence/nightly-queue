"""Fixed boot-bound qualification, separate from coding/backend readiness."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import socket
import threading
import time
from uuid import uuid4

from .artifacts import pack, unpack
from .client import Client, ExecutorRejected
from .contracts import Conflict, Manifest, Rejected, canonical, digest
from .runtime import durable


REQUIRED_CHECKS = {
    "task-uid-capabilities-image-control",
    "fresh-source-home-tmp-ipc",
    "egress-proxy-and-private-denial",
    "isolated-database-reset",
    "escaped-descendant-termination",
    "pause-resume-deadline",
    "pid-exhaustion-control-api",
    "bounded-logs-and-credential-isolation",
    "authenticated-retry-and-retention",
    "restart-reconciliation",
    "actual-cgroup-and-tmpfs-limits",
}
ARGV = ("/usr/local/bin/python", "-I", "-m", "vibearr_executor.slot_probe")
SYNTHETIC_KEY = "qualification-only-synthetic-key-0123456789"


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


def prepare(runtime, case, *, seconds=30, secret=""):
    base = next(iter(runtime.profiles.values()))
    profile = replace(
        base,
        profile_id="worker-qualification",
        argv=ARGV,
        backend_secret_env="PROBE_CREDENTIAL" if secret else "",
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
    if secret:
        runtime.supply_secret(operation, secret)
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
    (runtime.directory / "qualification.json").unlink(missing_ok=True)
    (runtime.directory / "qualification-progress.json").unlink(missing_ok=True)
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


def progress(runtime):
    if runtime.capabilities()["qualified"]:
        report = json.loads((runtime.directory / "qualification.json").read_bytes())
        return {**report, "state": "completed", "worker_boot": runtime.boot}
    path = runtime.directory / "qualification-progress.json"
    if path.exists():
        value = json.loads(path.read_bytes())
        if value.get("worker_boot") == runtime.boot:
            return value
    return {"state": "not-started", "worker_boot": runtime.boot}


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


def run_case(runtime, case, **options):
    identity = prepare(runtime, case, **options)
    runtime.start(identity)
    result = wait(runtime, identity)
    return identity, result


def passed(runtime, identity, result):
    if (
        result["state"] != "exited"
        or not result.get("descendants_terminated")
        or result["result"].get("exit_code") != 0
    ):
        raise Rejected(
            "Fixed boundary probe failed: "
            + runtime.logs(identity, 0, 8192).decode(errors="replace")
        )


def qualify(runtime):
    from .slot_safety import binding
    from .slot_processes import task_states

    current = binding(runtime)
    restart_receipt = check_restart(runtime)
    (runtime.directory / "qualification.json").unlink(missing_ok=True)
    durable(
        runtime.directory / "qualification-progress.json",
        canonical({"state": "running", "worker_boot": runtime.boot}),
    )
    checks = set()
    receipts = []
    listener = socket.socket()
    listener.bind(("0.0.0.0", 9001))
    listener.listen()
    listener.settimeout(0.1)
    stopping = threading.Event()

    def accept():
        while not stopping.is_set():
            try:
                connection, _ = listener.accept()
                connection.close()
            except TimeoutError:
                pass

    thread = threading.Thread(target=accept, daemon=True)
    thread.start()
    client = Client("http://127.0.0.1:8791", runtime.control_token, timeout=60)
    try:
        for _ in range(2):
            identity, result = run_case(runtime, "smoke")
            passed(runtime, identity, result)
            _, source = runtime.export(identity)
            if unpack(source)["payload.txt"][1] != b"verified task change\n":
                raise Rejected("Qualification source export differs.")
            receipts.append(result["receipt_digest"])
        checks.update(
            {
                "task-uid-capabilities-image-control",
                "fresh-source-home-tmp-ipc",
                "escaped-descendant-termination",
                "isolated-database-reset",
            }
        )
        identity, result = run_case(runtime, "network")
        passed(runtime, identity, result)
        checks.add("egress-proxy-and-private-denial")
        identity, result = run_case(runtime, "wait", seconds=3)
        if not result["result"].get("timed_out") or not result.get(
            "descendants_terminated"
        ):
            raise Rejected("Wall deadline did not terminate the task.")
        identity = prepare(runtime, "wait")
        runtime.start(identity)
        wait(runtime, identity, terminal=False)
        runtime.pause(identity)
        if not all(state in {"T", "t", "Z"} for state in task_states().values()):
            raise Rejected("Escaped descendants continued while paused.")
        runtime.resume(identity)
        runtime.stop(identity)
        checks.add("pause-resume-deadline")
        identity = prepare(runtime, "pids")
        runtime.start(identity)
        deadline = time.monotonic() + 20
        while b'"pid_exhausted": true' not in runtime.logs(identity, 0, 65536):
            if time.monotonic() >= deadline:
                raise Rejected("Task did not exhaust the actual slot PID ceiling.")
            time.sleep(0.05)
        if client.status(identity)["state"] != "running":
            raise Rejected("Control API failed under PID pressure.")
        result = client.stop(identity)
        if result["state"] != "exited" or not result.get("descendants_terminated"):
            raise Rejected("API cancellation failed under PID pressure.")
        checks.add("pid-exhaustion-control-api")
        identity, result = run_case(runtime, "flood")
        if not result["result"].get("log_truncated") or not result.get(
            "descendants_terminated"
        ):
            raise Rejected("Task output was not bounded.")
        if next(iter(runtime.profiles.values())).phase == "agent":
            identity, result = run_case(runtime, "credential", secret=SYNTHETIC_KEY)
            if SYNTHETIC_KEY.encode() in runtime.logs(identity, 0, 65536) or not result[
                "result"
            ].get("export_error"):
                raise Rejected(
                    "Selected credential escaped redaction or source filtering."
                )
        checks.add("bounded-logs-and-credential-isolation")
        bad = Client("http://127.0.0.1:8791", "invalid-qualification-token-0000000000")
        try:
            bad.capabilities()
        except ExecutorRejected as error:
            if error.http_status != 401:
                raise
        else:
            raise Rejected("Worker API accepted an invalid token.")
        runtime.purge(
            identity, runtime.record(identity)["manifest"]["run_id"], ["source", "logs"]
        )
        checks.update(
            {
                "authenticated-retry-and-retention",
                "restart-reconciliation",
                "actual-cgroup-and-tmpfs-limits",
            }
        )
        if checks != REQUIRED_CHECKS:
            raise Rejected("Qualification coverage is incomplete.")
        if canonical(current) != canonical(binding(runtime)):
            raise Rejected("Worker binding changed during qualification.")
        report = {
            "schema_version": 1,
            "binding_digest": digest(canonical(current)),
            "checks": sorted(checks),
            "restart_receipt": restart_receipt,
            "probe_receipts": receipts,
            "completed_at": datetime.now(timezone.utc).isoformat(),
        }
        durable(runtime.directory / "qualification.json", canonical(report))
        return {**report, "state": "completed", "worker_boot": runtime.boot}
    finally:
        if runtime.active:
            runtime.stop(runtime.active)
        runtime.qualifying = False
        runtime.probe_operation, runtime.probe_profile = "", None
        stopping.set()
        thread.join(timeout=1)
        listener.close()
        if not (runtime.directory / "qualification.json").exists():
            durable(
                runtime.directory / "qualification-progress.json",
                canonical({"state": "failed", "worker_boot": runtime.boot}),
            )
