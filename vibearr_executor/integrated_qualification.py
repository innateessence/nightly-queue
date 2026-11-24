"""Non-model qualification of the integrated runner's actual boot."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import time
from uuid import uuid4

from .artifacts import pack
from .contracts import Conflict, Rejected, canonical, digest
from .runtime import durable
from .slot_processes import task_states


PROBE = """import os, pathlib, socket, subprocess
assert os.getuid() == 1000
status = pathlib.Path('/proc/self/status').read_text()
assert 'CapEff:\\t0000000000000000' in status
for name in ['/logins', '/control', '/var/lib/vibearr', '/run/vibearr/runner']:
    try:
        list(pathlib.Path(name).iterdir())
    except PermissionError:
        pass
    else:
        raise AssertionError('trusted state exposed')
for host, port in [('127.0.0.1', 8791), ('1.1.1.1', 443)]:
    connection = socket.socket()
    connection.settimeout(1)
    try:
        connection.connect((host, port))
    except OSError:
        pass
    else:
        raise AssertionError('control or direct egress exposed')
    finally:
        connection.close()
assert not pathlib.Path('/home/task/previous-job').exists()
pathlib.Path('/home/task/previous-job').write_text('discard me')
subprocess.Popen(['/usr/local/bin/python', '-I', '-c', 'import time; time.sleep(120)'], start_new_session=True)
print('uid, state, egress, clean workspace and detached child probes passed')
"""


def qualify(runtime):
    if not runtime.qualification_lock.acquire(blocking=False):
        raise Conflict("Another qualification owns the runner.")
    try:
        if runtime.active or runtime.fenced:
            raise Conflict("Stop or reconcile active work before qualification.")
        from .slot_qualification import check_restart, prepare, wait

        restart_receipt = check_restart(runtime)
        witness = json.loads((runtime.directory / "restart-witness.json").read_bytes())
        reconciled = runtime.record(witness["operation_id"])
        if reconciled["result"].get("reconciled_boot") != runtime.boot:
            raise Rejected(
                "Prepare and interrupt a fresh witness; its reconciliation must bind this boot."
            )
        runtime.qualifying = True
        base = next(iter(runtime.profiles.values()))
        receipts = []
        for _attempt in range(2):
            operation_id = str(uuid4())
            profile = replace(
                base,
                profile_id="integrated-boundary-probe",
                argv=("/usr/local/bin/python", "-I", "-c", PROBE),
                network_hosts=(),
                backend_secret_env="",
                database_image="",
            )
            runtime.probe_operation, runtime.probe_profile = operation_id, profile
            source = pack(
                {"witness.txt": (0o644, b"Disposable qualification witness.\n")}
            )
            context = canonical({"qualification": True})
            runtime.upload("source", source, digest(source))
            runtime.upload("context", context, digest(context))
            manifest = {
                "schema_version": 1,
                "run_id": str(uuid4()),
                "operation_id": operation_id,
                "profile_id": profile.profile_id,
                "profile_digest": profile.digest,
                "source_digest": digest(source),
                "context_digest": digest(context),
                "phase": "agent",
                "limits": profile.data()["limits"],
                "deadline": (
                    datetime.now(timezone.utc) + timedelta(seconds=30)
                ).isoformat(),
                "network_hosts": [],
            }
            runtime.prepare(manifest)
            runtime.start(operation_id)
            deadline = time.monotonic() + 40
            while (
                runtime.status(operation_id)["state"] == "running"
                and time.monotonic() < deadline
            ):
                time.sleep(0.05)
            result = runtime.status(operation_id)
            if (
                result.get("result", {}).get("exit_code") != 0
                or not result.get("descendants_terminated")
                or task_states()
            ):
                runtime.stop(operation_id)
                raise RuntimeError(
                    "Integrated boundary probe failed; inspect its protected logs."
                )
            receipts.append(
                {
                    "operation_id": operation_id,
                    "receipt_digest": result["receipt_digest"],
                }
            )
            runtime.thread.join(timeout=5)
        deadline_operation = prepare(runtime, "wait", seconds=3)
        runtime.start(deadline_operation)
        deadline_result = wait(runtime, deadline_operation)
        runtime.thread.join(timeout=5)
        if not deadline_result.get("descendants_terminated") or not deadline_result.get(
            "result", {}
        ).get("timed_out"):
            raise Rejected("Deadline did not confirm descendant termination.")
        control_operation = prepare(runtime, "wait")
        runtime.start(control_operation)
        wait(runtime, control_operation, terminal=False)
        runtime.pause(control_operation)
        if not all(state in {"T", "t", "Z"} for state in task_states().values()):
            raise Rejected("Task descendants continued while paused.")
        runtime.resume(control_operation)
        canceled = runtime.stop(control_operation)
        runtime.thread.join(timeout=5)
        if (
            not canceled.get("descendants_terminated")
            or runtime.start(control_operation) != canceled
        ):
            raise Rejected("Cancellation or duplicate start replay was not terminal.")
        report = {
            "schema_version": 1,
            "runtime": "integrated-runner-v1",
            "binding_digest": runtime.execution_identity()["binding_digest"],
            "passed": True,
            "checks": [
                "container-ceilings",
                "task-uid-capabilities",
                "trusted-state-denial",
                "control-denial",
                "direct-egress-denial",
                "descendant-termination",
                "cross-job-reset",
                "pause-resume-deadline",
                "cancel-and-retry-reconciliation",
                "restart-reconciliation",
            ],
            "receipts": receipts,
            "restart_receipt": restart_receipt,
        }
        durable(runtime.directory / "integrated-qualification.json", canonical(report))
        return report
    finally:
        runtime.qualifying = False
        runtime.probe_operation, runtime.probe_profile = "", None
        runtime.qualification_lock.release()
