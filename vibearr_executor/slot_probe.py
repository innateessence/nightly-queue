"""Fixed, credential-free task used only by boundary qualification."""

import ctypes
import errno
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
    try:
        socket.create_connection(("127.0.0.1", 8791), timeout=0.5).close()
    except OSError:
        pass
    else:
        raise AssertionError("Task reached its trusted worker API.")
    with socket.socket() as local:
        local.bind(("127.0.0.1", 0))
        local.listen()
        with socket.create_connection(local.getsockname(), timeout=1):
            pass
    if os.environ.get("DATABASE_URL"):
        import psycopg2

        with psycopg2.connect(os.environ["DATABASE_URL"]) as database:
            with database.cursor() as cursor:
                cursor.execute("SELECT to_regclass('qualification_previous_job')")
                assert cursor.fetchone()[0] is None, "Previous job database leaked."
                cursor.execute("CREATE TABLE qualification_previous_job (value text)")
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
        time.sleep(3600)
    elif case == "memory":
        Path("/proc/self/oom_score_adj").write_text("1000")
        allocated = []
        limit = int(Path("/sys/fs/cgroup/memory.max").read_text())
        print(json.dumps({"memory_exhaustion_started": True}), flush=True)
        while len(allocated) * 8 * 1024**2 <= limit * 2:
            allocated.append(bytearray(8 * 1024**2))
        raise AssertionError("Task exceeded the actual memory ceiling without OOM.")
    elif case == "flood":
        print("x" * (2 * 1024 * 1024), flush=True)
    elif case == "pids":
        children = []
        try:
            while True:
                children.append(
                    subprocess.Popen(
                        ["/usr/local/bin/python", "-c", "import time; time.sleep(60)"],
                        start_new_session=True,
                    )
                )
        except OSError as error:
            assert error.errno == errno.EAGAIN
        print(json.dumps({"pid_exhausted": True}), flush=True)
        time.sleep(60)


if __name__ == "__main__":
    main()
