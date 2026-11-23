"""Model-free probes of the actual compact worker boot."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import socket
import time
from uuid import uuid4

from .artifacts import pack
from .client import Client, ExecutorRejected
from .compact_protocol import PREFIX, qualification_targets as service_targets
from .contracts import Conflict, Rejected, canonical, digest
from .control_token import read_control_token
from .runtime import durable
from .slot_processes import task_states
from .slot_qualification import check_restart, prepare, wait


RUNTIME = "compact-worker-v1"
REQUIRED_CHECKS = {
    "container-ceilings",
    "task-uid-capabilities",
    "trusted-state-denial",
    "runner-worker-separation",
    "runner-control-denial",
    "production-network-denial",
    "direct-egress-denial",
    "descendant-termination",
    "cross-phase-reset",
    "local-database-privilege",
    "local-database-reset",
    "pause-resume-deadline",
    "cancel-and-retry-reconciliation",
    "pid-exhaustion-control-api",
    "bounded-logs",
    "authenticated-control",
    "restart-reconciliation",
}

PROBE = r"""import os,pathlib,socket,subprocess
assert os.getuid() == os.geteuid() == 1000
assert os.getgroups() == []
status = dict(line.split(':',1) for line in pathlib.Path('/proc/self/status').read_text().splitlines() if ':' in line)
assert int(status['CapEff'].strip(),16) == 0
assert int(status['CapBnd'].strip(),16) == 0
assert status['NoNewPrivs'].strip() == '1'
for name in ['/logins','/control','/var/lib/vibearr','/run/vibearr/runner','/run/vibearr/worker']:
    try:
        list(pathlib.Path(name).iterdir())
    except (PermissionError,FileNotFoundError):
        pass
    else:
        raise AssertionError('trusted state exposed: ' + name)
for host,port in [('127.0.0.1',8791),('__RUNNER__',8792),('__RUNNER__',3128),('__RUNNER__',3130),('__APP__',8000),('__POSTGRES__',5432),('169.254.169.254',80),('1.1.1.1',443)]:
    try:
        socket.create_connection((host,port),timeout=0.5).close()
    except OSError:
        pass
    else:
        raise AssertionError('forbidden network destination exposed')
with socket.socket() as local:
    local.bind(('127.0.0.1',0))
    local.listen()
    with socket.create_connection(local.getsockname(),timeout=1):
        pass
for name in ['/home/task/previous-job','/tmp/compact-previous-phase','/dev/shm/compact-previous-phase']:
    path = pathlib.Path(name)
    assert not path.exists(), 'previous phase data survived'
    path.write_text('discard before reuse')
subprocess.Popen(['/usr/local/bin/python','-I','-c','import time; time.sleep(120)'],start_new_session=True)
print('compact boundary and fresh phase probes passed',flush=True)
"""

DATABASE_PROBE = r"""import os,pathlib,psycopg2
assert os.getuid() == 1000
assert not os.environ.get('VIBEARR_BACKEND_SECRET_FILE')
assert not os.environ.get('HTTP_PROXY') and not os.environ.get('HTTPS_PROXY')
with psycopg2.connect(os.environ['DATABASE_URL']) as database:
    with database.cursor() as cursor:
        cursor.execute('SELECT rolsuper,rolcreatedb,rolcreaterole,rolreplication,rolbypassrls FROM pg_roles WHERE rolname=current_user')
        assert cursor.fetchone() == (False,False,False,False,False), 'task database role has administrative authority'
        cursor.execute("SELECT to_regclass('compact_previous_phase')")
        assert cursor.fetchone()[0] is None, 'previous phase database survived'
        cursor.execute('CREATE TABLE compact_previous_phase(value text)')
for name in ['/test-database','/run/postgresql-admin']:
    try:
        list(pathlib.Path(name).iterdir())
    except (PermissionError,FileNotFoundError):
        pass
    else:
        raise AssertionError('database administrator storage exposed')
print('local database least privilege and reset passed',flush=True)
"""


def run_probe(runtime, code, *, database=False):
    base = next(iter(runtime.profiles.values()))
    profile = replace(
        base,
        profile_id="compact-boundary-probe",
        argv=("/usr/local/bin/python", "-I", "-c", code),
        phase="validation" if database else "agent",
        network_hosts=(),
        backend_secret_env="",
        database_image=base.image if database else "",
    )
    operation = str(uuid4())
    runtime.probe_operation, runtime.probe_profile = operation, profile
    source = pack({"witness.txt": (0o644, b"Compact qualification witness.\n")})
    context = canonical({"qualification": True})
    runtime.upload("source", source, digest(source))
    runtime.upload("context", context, digest(context))
    runtime.prepare(
        {
            "schema_version": 1,
            "run_id": str(uuid4()),
            "operation_id": operation,
            "profile_id": profile.profile_id,
            "profile_digest": profile.digest,
            "source_digest": digest(source),
            "context_digest": digest(context),
            "phase": profile.phase,
            "limits": profile.data()["limits"],
            "deadline": (
                datetime.now(timezone.utc) + timedelta(seconds=40)
            ).isoformat(),
            "network_hosts": [],
        }
    )
    runtime.start(operation)
    result = wait(runtime, operation)
    runtime.thread.join(timeout=5)
    require_exit(runtime, operation, result)
    return result


def require_exit(runtime, operation, result):
    if (
        result.get("state") != "exited"
        or result.get("result", {}).get("exit_code") != 0
        or result.get("descendants_terminated") is not True
        or task_states()
    ):
        if runtime.active:
            runtime.stop(operation)
        raise Rejected(
            "Compact boundary probe failed; inspect protected operation logs."
        )


def lost_start(runtime, operation, token):
    path = PREFIX + "/v1/operations/" + operation + "/start"
    headers = (
        "POST "
        + path
        + " HTTP/1.1\r\nHost: 127.0.0.1\r\nAuthorization: Bearer "
        + token
        + "\r\nContent-Type: application/json\r\nContent-Length: 2\r\n\r\n{}"
    )
    with socket.create_connection(("127.0.0.1", 8791), timeout=5) as connection:
        connection.sendall(headers.encode())
        connection.shutdown(socket.SHUT_WR)
    client = Client("http://127.0.0.1:8791", token)
    deadline = time.monotonic() + 10
    while runtime.record(operation).get("state") != "running":
        client.status(operation)
        if time.monotonic() >= deadline:
            raise Rejected("Lost-response start was not durably reconciled.")
        time.sleep(0.05)
    record = runtime.record(operation)
    client.start(operation)
    if runtime.record(operation) != record:
        raise Rejected("A retry after a lost response changed the running operation.")


def qualification_targets(value):
    value = service_targets(value)
    if value["runner"] not in {
        entry[4][0] for entry in socket.getaddrinfo("runner", 8792, socket.AF_INET)
    }:
        raise Rejected(
            "Qualification runner address differs from the live trusted service."
        )
    return dict(value)


def qualify(runtime, targets=None):
    if not runtime.qualification_lock.acquire(blocking=False):
        raise Conflict("Another qualification owns the worker.")
    report_path = runtime.directory / "compact-qualification.json"
    owns_operation = False
    try:
        if runtime.active or runtime.fenced:
            raise Conflict("Stop or reconcile active work before qualification.")
        targets = qualification_targets(targets)
        token = read_control_token(Path(os.environ["VIBEARR_EXECUTOR_TOKEN_FILE"]))
        identity = runtime.execution_identity()
        if identity.get("runtime") != RUNTIME:
            raise Rejected(
                "Compact qualification requires the separate compact worker."
            )
        with socket.create_connection((targets["runner"], 8792), timeout=5):
            pass
        control = Client("http://127.0.0.1:8791", token).capabilities()
        if control.get("worker_boot") != runtime.boot:
            raise Rejected(
                "Trusted control positive witness differs from this worker boot."
            )
        restart_receipt = check_restart(runtime)
        witness = json.loads((runtime.directory / "restart-witness.json").read_bytes())
        record = runtime.record(witness["operation_id"])
        if record.get("result", {}).get("reconciled_boot") != runtime.boot:
            raise Rejected("Restart witness does not bind the current worker boot.")
        report_path.unlink(missing_ok=True)
        runtime.qualifying = True
        owns_operation = True
        receipts = []
        code = PROBE
        for name, address in targets.items():
            code = code.replace("__" + name.upper() + "__", address)
        for _attempt in range(2):
            receipts.append(run_probe(runtime, code))
            receipts.append(run_probe(runtime, DATABASE_PROBE, database=True))
        operation = prepare(runtime, "wait", seconds=3)
        runtime.start(operation)
        result = wait(runtime, operation)
        runtime.thread.join(timeout=5)
        if not result.get("descendants_terminated") or not result.get("result", {}).get(
            "timed_out"
        ):
            raise Rejected("Deadline did not terminate every task descendant.")
        receipts.append(result)
        operation = prepare(runtime, "wait")
        lost_start(runtime, operation, token)
        wait(runtime, operation, terminal=False)
        runtime.pause(operation)
        if not all(state in {"T", "t", "Z"} for state in task_states().values()):
            raise Rejected("Task descendants continued while paused.")
        runtime.resume(operation)
        result = runtime.stop(operation)
        runtime.thread.join(timeout=5)
        if (
            result.get("descendants_terminated") is not True
            or runtime.start(operation) != result
        ):
            raise Rejected("Cancellation or duplicate start was not terminal.")
        receipts.append(result)
        operation = prepare(runtime, "pids")
        runtime.start(operation)
        deadline = time.monotonic() + 25
        while b'"pid_exhausted": true' not in runtime.logs(operation, 0, 65536):
            if time.monotonic() >= deadline:
                raise Rejected("Task did not reach the actual worker PID ceiling.")
            time.sleep(0.05)
        client = Client("http://127.0.0.1:8791", token)
        if client.status(operation).get("state") != "running":
            raise Rejected("Worker control failed under PID pressure.")
        result = client.stop(operation)
        runtime.thread.join(timeout=5)
        if result.get("descendants_terminated") is not True:
            raise Rejected("PID exhaustion cancellation did not confirm cleanup.")
        receipts.append(result)
        memory_events = Path("/sys/fs/cgroup/memory.events")
        before_oom = dict(
            line.split() for line in memory_events.read_text().splitlines()
        )
        operation = prepare(runtime, "memory")
        runtime.start(operation)
        result = wait(runtime, operation)
        runtime.thread.join(timeout=5)
        after_oom = dict(
            line.split() for line in memory_events.read_text().splitlines()
        )
        if (
            result.get("result", {}).get("exit_code") != -9
            or not result.get("descendants_terminated")
            or int(after_oom["oom_kill"]) <= int(before_oom["oom_kill"])
            or Client("http://127.0.0.1:8791", token).status(operation).get("state")
            != "exited"
        ):
            raise Rejected(
                "Actual cgroup OOM did not terminate the task with responsive control."
            )
        receipts.append(result)
        operation = prepare(runtime, "flood")
        runtime.start(operation)
        result = wait(runtime, operation)
        runtime.thread.join(timeout=5)
        if not result.get("result", {}).get("log_truncated") or not result.get(
            "descendants_terminated"
        ):
            raise Rejected("Task output was not bounded with confirmed cleanup.")
        receipts.append(result)
        bad = Client(
            "http://127.0.0.1:8791", "invalid-compact-qualification-token-0000"
        )
        try:
            bad.capabilities()
        except ExecutorRejected as error:
            if error.http_status != 401:
                raise
        else:
            raise Rejected("Worker control accepted an invalid token.")
        if identity != runtime.execution_identity():
            raise Rejected("Worker binding changed during qualification.")
        report = {
            "schema_version": 1,
            "kind": "actual-worker-boundary",
            "runtime": RUNTIME,
            **{
                key: identity[key]
                for key in ("binding_digest", "instance_id", "worker_boot")
            },
            "image_digest": next(iter(runtime.profiles.values())).image,
            "passed": True,
            "fixtures": False,
            "checks": sorted(REQUIRED_CHECKS),
            "receipts": [
                {
                    "operation_id": value["operation_id"],
                    "receipt_digest": value["receipt_digest"],
                }
                for value in receipts
            ],
            "restart_receipt": restart_receipt,
            "targets": targets,
            "resources": {
                "memory_max": Path("/sys/fs/cgroup/memory.max").read_text().strip(),
                "memory_peak": Path("/sys/fs/cgroup/memory.peak").read_text().strip(),
                "oom_kill_before": int(before_oom["oom_kill"]),
                "oom_kill_after": int(after_oom["oom_kill"]),
                "pids_max": Path("/sys/fs/cgroup/pids.max").read_text().strip(),
                "pids_peak": Path("/sys/fs/cgroup/pids.peak").read_text().strip(),
            },
            "completed_at": datetime.now(timezone.utc).isoformat(),
        }
        durable(report_path, canonical(report))
        return report
    finally:
        if owns_operation and runtime.active:
            runtime.stop(runtime.active)
        runtime.qualifying = False
        runtime.probe_operation, runtime.probe_profile = "", None
        runtime.qualification_lock.release()
