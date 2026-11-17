import os
from pathlib import Path
import signal
import time


def task_states():
    result = {}
    for path in Path("/proc").iterdir():
        if not path.name.isdigit():
            continue
        try:
            fields = dict(
                line.split(":", 1)
                for line in (path / "status").read_text().splitlines()
                if ":" in line
            )
            if fields["Uid"].split()[0] == "1000":
                result[int(path.name)] = fields["State"].split()[0]
        except (FileNotFoundError, ProcessLookupError):
            continue
    return result


def signal_tasks(signum):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        current = task_states()
        for identity in current:
            try:
                os.kill(identity, signum)
            except ProcessLookupError:
                pass
        if signum != signal.SIGSTOP:
            return
        if all(state in {"T", "t", "Z"} for state in task_states().values()):
            return
        time.sleep(0.01)
    raise RuntimeError("Task descendants could not all be paused.")
