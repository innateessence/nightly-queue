"""Fixed, credential-free task used only by boundary qualification."""

import ctypes
import json
import os
from pathlib import Path
import socket
import subprocess
import time


def main():
    context = json.loads(Path(os.environ["VIBEARR_CONTEXT"]).read_bytes())
    status = dict(
        line.split(":", 1)
        for line in Path("/proc/self/status").read_text().splitlines()
        if ":" in line
    )
    assert os.getuid() == os.geteuid() == 1000
    assert os.getgroups() == []
    assert int(status["CapEff"].strip(), 16) == 0
    assert int(status["CapBnd"].strip(), 16) == 0
    assert status["NoNewPrivs"].strip() == "1"
    for filename in (
        "/control/instance",
        "/control/sentinel",
        "/run/vibearr/control-token",
    ):
        try:
            Path(filename).read_bytes()
        except (PermissionError, FileNotFoundError):
            pass
        else:
            raise AssertionError("Task read private worker control state.")
    try:
        Path("/opt/worker-injection").write_text("injection")
    except (PermissionError, OSError):
        pass
    else:
        raise AssertionError("Task wrote the immutable worker image.")
    for filename in (
        "/home/task/previous-job",
        "/tmp/vibearr-task-cache",
        "/dev/shm/vibearr-cache",
    ):
        path = Path(filename)
        assert not path.exists(), "Previous job data leaked into this task."
        path.write_text("previous secret")
    for destination in ("127.0.0.1", "169.254.169.254", "1.1.1.1"):
        try:
            with socket.create_connection((destination, 9001), timeout=0.5):
                pass
        except OSError:
            pass
        else:
            raise AssertionError("Task reached a forbidden network destination.")
    libc = ctypes.CDLL(None, use_errno=True)
    for path in Path("/proc/sysvipc").iterdir():
        lines = path.read_text().splitlines()
        columns = lines[0].split()
        assert all(
            dict(zip(columns, line.split()))["uid"] != "1000" for line in lines[1:]
        ), "Previous job IPC leaked."
    segment = libc.shmget(0, 4096, 0o1600)
    Path("/work/payload.txt").write_text("verified task change\n")
    escaped = subprocess.Popen(
        ["/usr/local/bin/python", "-c", "import os,time; os.setsid(); time.sleep(60)"]
    )
    print(
        json.dumps(
            {
                "isolated": True,
                "uid": os.getuid(),
                "escaped_pid": escaped.pid,
                "sysv_segment": segment,
            }
        ),
        flush=True,
    )
    case = context.get("case", "smoke")
    if case == "wait":
        time.sleep(60)
    elif case == "flood":
        print("x" * (2 * 1024 * 1024), flush=True)
    elif case == "credential":
        secret = Path(os.environ["VIBEARR_BACKEND_SECRET_FILE"]).read_text()
        print(secret, flush=True)
        Path("/work/leaked-key.txt").write_text(secret)


if __name__ == "__main__":
    main()
