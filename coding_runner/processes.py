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

current_tracker: contextvars.ContextVar[Any] = contextvars.ContextVar(
    "runner_process_tracker", default=None
)


def identity(pid: int) -> tuple[str, int, int] | None:
    try:
        raw = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return raw[0], int(raw[2]), int(raw[19])
    except FileNotFoundError:
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

    def spawn(self, arguments, **kwargs):
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
            os.write(writer, b"1")
            process.runner_operation = operation
            return process
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
        os.killpg(pid, signal.SIGKILL)
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


def popen(arguments, **kwargs):
    tracker = current_tracker.get()
    return (
        tracker.spawn(arguments, **kwargs)
        if tracker
        else subprocess.Popen(arguments, **kwargs)
    )


def reaped(process):
    tracker = current_tracker.get()
    if tracker:
        tracker.reaped(process)


def run(
    arguments, *, input=None, capture_output=False, timeout=None, check=False, **kwargs
):
    if not current_tracker.get():
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
    with popen(arguments, **kwargs) as process:
        try:
            stdout, stderr = process.communicate(input, timeout=timeout)
        except BaseException:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
            reaped(process)
            raise
        reaped(process)
        result = subprocess.CompletedProcess(
            arguments, process.returncode, stdout, stderr
        )
        if check:
            result.check_returncode()
        return result
