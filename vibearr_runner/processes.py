import contextvars
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from typing import Any
from uuid import uuid4

from .journal import Journal, JournalError
from .backends import BackendInterrupted
from .budgets import BudgetExceeded

current_tracker: contextvars.ContextVar[Any] = contextvars.ContextVar(
    "runner_process_tracker", default=None
)

current_execution: contextvars.ContextVar[Any] = contextvars.ContextVar(
    "runner_process_execution_limits", default=None
)


def identity(pid: int) -> tuple[str, int, int] | None:
    try:
        raw = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return raw[0], int(raw[2]), int(raw[19])
    except (FileNotFoundError, ProcessLookupError):
        return None


def group_alive(pid: int) -> bool:
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        details = identity(int(entry.name))
        if details and details[0] != "Z" and details[1] == pid:
            return True
    return False


class ProcessTracker:
    def __init__(self, journal: Journal, state: dict[str, Any]):
        self.journal, self.state = journal, state
        self.boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()

    def spawn(self, arguments, *, before_release=None, **kwargs):
        if kwargs.get("shell") or isinstance(arguments, (str, bytes)):
            raise JournalError(
                "Tracked processes require trusted argv without shell interpretation."
            )
        records = self.state.setdefault("processes", {})
        operation = str(uuid4())
        record = {"status": "launching", "boot": self.boot, "pid": None, "start": None}
        records[operation] = record
        self.journal.save(self.state)
        reader, writer = os.pipe2(os.O_CLOEXEC)
        process = None
        released = False
        try:
            passed = tuple(kwargs.pop("pass_fds", ())) + (reader,)
            kwargs["start_new_session"] = True
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-I",
                    str(Path(__file__).with_name("process_worker.py")),
                    str(reader),
                    *arguments,
                ],
                pass_fds=passed,
                **kwargs,
            )
            details = identity(process.pid)
            if details is None:
                raise JournalError("Process exited before durable launch confirmation.")
            record.update(
                {"pid": process.pid, "start": details[2], "status": "running"}
            )
            self.journal.save(self.state)
            process.runner_operation = operation
            if before_release is not None:
                before_release()
            os.write(writer, b"1")
            released = True
            return process
        except BaseException:
            if process is not None and not released:
                process.kill()
                process.wait()
                self.reaped(process)
                for stream in (process.stdin, process.stdout, process.stderr):
                    if stream is not None:
                        stream.close()
            raise
        finally:
            os.close(reader)
            os.close(writer)

    def reaped(self, process):
        operation = getattr(process, "runner_operation", None)
        if operation is not None:
            record = self.state["processes"][operation]
            if group_alive(record["pid"]):
                self._terminate(record)
            record.update({"status": "stopped", "returncode": process.returncode})
            self.journal.save(self.state)

    def _terminate(self, record):
        pid = record["pid"]
        if record["boot"] != self.boot or pid is None:
            return
        details = identity(pid)
        if details and (details[1] != pid or details[2] != record["start"]):
            if not group_alive(pid):
                return
            raise JournalError(
                "Process identity changed; operator reconciliation is required."
            )
        if not group_alive(pid):
            return
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            if group_alive(pid):
                raise JournalError(
                    "Child termination is unconfirmed; execution remains fenced."
                ) from None
            return
        deadline = time.monotonic() + 2
        while group_alive(pid):
            if time.monotonic() >= deadline:
                raise JournalError(
                    "Child termination is unconfirmed; execution remains fenced."
                )
            time.sleep(0.02)

    def recover(self):
        for record in self.state.get("processes", {}).values():
            if record["status"] == "stopped":
                continue
            self._terminate(record)
            record["status"] = "stopped"
        self.journal.save(self.state)


def popen(arguments, *, before_release=None, **kwargs):
    tracker = current_tracker.get()
    if before_release is not None and tracker is None:
        raise JournalError("Mutation dispatch requires a tracked process launch gate.")
    return (
        tracker.spawn(arguments, before_release=before_release, **kwargs)
        if tracker
        else subprocess.Popen(arguments, **kwargs)
    )


def reaped(process):
    tracker = current_tracker.get()
    if tracker:
        tracker.reaped(process)


def run(
    arguments,
    *,
    input=None,
    capture_output=False,
    timeout=None,
    check=False,
    on_launch=None,
    **kwargs,
):
    execution = current_execution.get()
    if on_launch is not None and current_tracker.get() is None:
        raise JournalError("Mutation dispatch requires a tracked process launch gate.")
    if not current_tracker.get() and execution is None:
        return subprocess.run(
            arguments,
            input=input,
            capture_output=capture_output,
            timeout=timeout,
            check=check,
            **kwargs,
        )
    if capture_output:
        kwargs.update(stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if input is not None:
        kwargs["stdin"] = subprocess.PIPE
    if execution:
        kwargs["start_new_session"] = True
        deadline, local_checkpoint, checkpoint = execution
        if not local_checkpoint() or checkpoint() is not True:
            raise BackendInterrupted(
                "Git preparation interrupted by execution controls."
            )
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise BudgetExceeded("Git preparation exhausted its active budget.")
        timeout = remaining if timeout is None else min(timeout, remaining)

    def release():
        if execution:
            deadline, local_checkpoint, checkpoint = execution
            if not local_checkpoint() or checkpoint() is not True:
                raise BackendInterrupted(
                    "Mutation launch interrupted by execution controls."
                )
            if time.monotonic() >= deadline:
                raise BudgetExceeded("Mutation launch exhausted its active budget.")
        on_launch()

    if on_launch is not None:
        kwargs["before_release"] = release
    with popen(arguments, **kwargs) as process:
        try:
            if execution is None:
                stdout, stderr = process.communicate(input, timeout=timeout)
            else:
                expires = min(time.monotonic() + timeout, deadline)
                pending_input = input
                while True:
                    if not local_checkpoint():
                        raise BackendInterrupted(
                            "Git preparation interrupted by execution controls."
                        )
                    remaining = expires - time.monotonic()
                    if remaining <= 0:
                        if time.monotonic() >= deadline:
                            raise BudgetExceeded(
                                "Git preparation exhausted its active budget."
                            )
                        raise subprocess.TimeoutExpired(arguments, timeout)
                    try:
                        stdout, stderr = process.communicate(
                            pending_input, timeout=min(0.1, remaining)
                        )
                        break
                    except subprocess.TimeoutExpired:
                        pending_input = None
        except BaseException:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            finally:
                process.wait()
                reaped(process)
            raise
        reaped(process)
        result = subprocess.CompletedProcess(
            arguments, process.returncode, stdout, stderr
        )
        if check:
            result.check_returncode()
        if execution and (not local_checkpoint() or checkpoint() is not True):
            raise BackendInterrupted(
                "Git preparation interrupted by execution controls."
            )
        return result
