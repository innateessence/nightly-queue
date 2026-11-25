"""Confirm descendant termination before reusing the worker boundary."""

import os
from pathlib import Path
import signal
import time


def terminate_children() -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        children = []
        for path in Path("/proc").iterdir():
            if not path.name.isdigit():
                continue
            try:
                status = (path / "status").read_text()
                uid = next(
                    line.split()[1]
                    for line in status.splitlines()
                    if line.startswith("Uid:")
                )
                if uid == "1000":
                    children.append(int(path.name))
            except (FileNotFoundError, ProcessLookupError):
                continue
        for child in children:
            try:
                os.kill(child, signal.SIGKILL)
            except ProcessLookupError:
                pass
        while True:
            try:
                if os.waitpid(-1, os.WNOHANG)[0] == 0:
                    break
            except ChildProcessError:
                break
        if not children:
            return
        time.sleep(0.01)
    raise RuntimeError("Task descendants did not terminate.")
