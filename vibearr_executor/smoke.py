"""Fixed qualification recipes, not a coding adapter or model simulator."""

import errno
import json
import os
import signal
import socket
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


def check(value, reason):
    if not value:
        raise RuntimeError(reason)


def denied(address, port, family=socket.AF_INET):
    with socket.socket(family, socket.SOCK_STREAM) as stream:
        stream.settimeout(0.3)
        check(
            stream.connect_ex((address, port)) != 0,
            f"Forbidden network connection succeeded: {address}:{port}",
        )


def boundary():
    check(sys.version_info[:2] == (3, 12), "Python 3.12 is required")
    check(os.getuid() == 1000, "Task must have a separate unprivileged UID")
    status = Path("/proc/self/status").read_text()
    check("CapEff:\t0000000000000000" in status, "Task has effective capabilities")
    check("NoNewPrivs:\t1" in status, "Task can acquire privileges")
    check("Seccomp:\t2" in status, "Task has no seccomp filter")
    for path in ("/input/secret", "/input/job.json", "/state", "/proc/1/environ"):
        try:
            with open(path, "rb") as stream:
                stream.read(1)
        except (PermissionError, IsADirectoryError):
            continue
        raise RuntimeError("Task can read trusted executor state: " + path)
    for path in ("/state/result.json", "/state/booted", "/input/job.json"):
        try:
            Path(path).write_text('{"exit_code":0,"terminated":true}')
        except PermissionError:
            continue
        except OSError as exc:
            check(exc.errno == errno.EROFS, "Unexpected protected-state write outcome")
            continue
        raise RuntimeError("Task can forge trusted execution evidence: " + path)
    print(
        '{"exit_code":999,"terminated":false,"artifact":{"result_digest":"forged"}}',
        flush=True,
    )
    for path in (
        "/var/run/docker.sock",
        "/run/user/1000/docker.sock",
        "/home/jack/dockers/nightly-queue",
        "/var/lib/vibearr-runner",
        "/var/lib/coding-runner",
    ):
        check(not Path(path).exists(), "Host/control state was mounted: " + path)
    for key in (
        "NQ_API_TOKEN",
        "VIBEARR_API_TOKEN",
        "DATABASE_PASSWORD",
        "DJANGO_SECRET_KEY",
        "AWS_ACCESS_KEY_ID",
        "GITHUB_TOKEN",
        "GH_TOKEN",
        "DOCKER_HOST",
        "CREDENTIAL_ENCRYPTION_KEY",
    ):
        check(key not in os.environ, "Supervisor credential was inherited")
    check(
        subprocess.check_output(
            ["git", "rev-parse", "--show-toplevel"], text=True
        ).strip()
        == "/work",
        "Disposable Git metadata is missing",
    )
    try:
        Path("/opt/executor/injected").write_text("no")
    except OSError as exc:
        check(
            exc.errno in (errno.EROFS, errno.EACCES),
            "Unexpected root filesystem failure",
        )
    else:
        raise RuntimeError("Task image is writable")
    try:
        os.kill(1, signal.SIGKILL)
    except PermissionError:
        pass
    else:
        raise RuntimeError("Task can kill trusted PID 1")
    Path("added.txt").write_text("isolated addition\n")
    Path("executable").write_text("#!/bin/sh\nexit 0\n")
    Path("executable").chmod(0o755)
    Path("delete.txt").unlink()
    Path("original.txt").write_text("changed\n")
    print(
        json.dumps(
            {
                "python": sys.version.split()[0],
                "git": subprocess.check_output(["git", "--version"], text=True).strip(),
            }
        ),
        flush=True,
    )


def network(*, install=False, model=False):
    gateways = {
        "172.17.0.1",
        "10.0.0.1",
        "192.168.1.1",
        "169.254.169.254",
        "1.1.1.1",
        "127.0.0.11",
    }
    for line in Path("/proc/net/route").read_text().splitlines()[1:]:
        fields = line.split()
        destination = struct.unpack("<I", bytes.fromhex(fields[1]))[0]
        mask = struct.unpack("<I", bytes.fromhex(fields[7]))[0]
        if destination and mask:
            gateways.add(socket.inet_ntoa(struct.pack("<I", destination + 1)))
    for address in sorted(gateways):
        for port in (53, 443, 5432, 6379, 7777, 8791, 32771):
            denied(address, port)
    for address in ("::1", "fd00::1", "fe80::1", "2606:4700:4700::1111"):
        denied(address, 443, socket.AF_INET6)
    with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as listener:
        try:
            listener.bind(("::1", 0))
            listener.listen()
        except OSError:
            check(
                Path("/proc/sys/net/ipv6/conf/all/disable_ipv6").read_text().strip()
                == "1",
                "IPv6 loopback could not be tested or proven disabled",
            )
        else:
            denied("::1", listener.getsockname()[1], socket.AF_INET6)
    proxy = os.environ.get("HTTPS_PROXY")
    if not proxy:
        denied("151.101.0.223", 443)
        print("offline-network-denied", flush=True)
        return
    for destination in (
        "localhost:443",
        "127.0.0.1:443",
        "169.254.169.254:443",
        "example.com:443",
        "pypi.org:80",
        "[::1]:443",
    ):
        address = proxy.removeprefix("http://").split(":")
        with socket.create_connection(
            (address[0], int(address[1])), timeout=5
        ) as stream:
            stream.sendall(
                f"CONNECT {destination} HTTP/1.1\r\nHost: {destination}\r\n\r\n".encode()
            )
            check(
                stream.recv(4096).startswith(b"HTTP/1.1 403"),
                "Proxy accepted forbidden destination",
            )
    if model:
        try:
            urllib.request.urlopen("https://api.openai.com/v1/models", timeout=30)
        except urllib.error.HTTPError as exc:
            check(exc.code in (401, 403), "Unexpected model endpoint response")
        print("allowlisted-model-egress", flush=True)
    if not install:
        return
    with urllib.request.urlopen(
        "https://pypi.org/simple/idna/", timeout=30
    ) as response:
        check(response.status == 200, "Allowed HTTPS endpoint is unreachable")
    subprocess.run(
        [
            sys.executable,
            "-I",
            "-m",
            "pip",
            "--disable-pip-version-check",
            "install",
            "--no-cache-dir",
            "--no-deps",
            "--index-url",
            "https://pypi.org/simple",
            "--target",
            "/home/task/packages",
            "idna==3.11",
        ],
        check=True,
        timeout=90,
    )
    check(Path("/home/task/packages/idna").is_dir(), "Dependency installation failed")
    print("credential-free-allowlisted-package-install", flush=True)


def limits():
    check(
        Path("/sys/fs/cgroup/memory.max").read_text().strip() == str(256 * 1024 * 1024),
        "Memory limit not applied",
    )
    check(
        Path("/sys/fs/cgroup/pids.max").read_text().strip() == "32",
        "PID limit not applied",
    )
    quota, period = map(int, Path("/sys/fs/cgroup/cpu.max").read_text().split())
    check(quota == period, "CPU quota not applied")
    size = os.statvfs("/work")
    check(
        size.f_blocks * size.f_frsize == 64 * 1024 * 1024,
        "Workspace capacity not enforced",
    )
    try:
        with open("fill", "wb") as stream:
            for _ in range(80):
                stream.write(b"x" * 1024 * 1024)
    except OSError as exc:
        check(exc.errno == errno.ENOSPC, "Unexpected workspace capacity failure")
    else:
        raise RuntimeError("Workspace exceeded its registered capacity")
    Path("fill").unlink()
    children = []
    try:
        for _ in range(80):
            child = os.fork()
            if child == 0:
                time.sleep(30)
                os._exit(0)
            children.append(child)
    except OSError as exc:
        check(exc.errno == errno.EAGAIN, "Unexpected PID limit failure")
    else:
        raise RuntimeError("PID limit was not enforced")
    finally:
        for child in children:
            os.kill(child, signal.SIGKILL)
            os.waitpid(child, 0)
    process = subprocess.run(
        [sys.executable, "-I", "-c", "x = bytearray(512 * 1024 * 1024)"], timeout=20
    )
    check(
        process.returncode == -signal.SIGKILL,
        "Memory exhaustion did not kill the allocating child",
    )
    check(
        "oom_kill 0" not in Path("/sys/fs/cgroup/memory.events").read_text(),
        "No real cgroup OOM evidence",
    )
    before = Path("/sys/fs/cgroup/cpu.stat").read_text()
    command = [
        sys.executable,
        "-I",
        "-c",
        "import time\nend=time.monotonic()+1.5\nwhile time.monotonic()<end: pass",
    ]
    workers = [subprocess.Popen(command) for _ in range(4)]
    for worker in workers:
        check(worker.wait(timeout=10) == 0, "CPU contention worker failed")
    after = Path("/sys/fs/cgroup/cpu.stat").read_text()
    previous = int(
        next(
            line.split()[1]
            for line in before.splitlines()
            if line.startswith("nr_throttled ")
        )
    )
    current = int(
        next(
            line.split()[1]
            for line in after.splitlines()
            if line.startswith("nr_throttled ")
        )
    )
    check(current > previous, "CPU contention did not trigger real cgroup throttling")
    print("memory-pids-workspace-enforced", flush=True)


def main():
    kind = sys.argv[1]
    if kind == "sleep":
        for _ in range(2):
            if os.fork() == 0:
                os.setsid()
                while True:
                    time.sleep(1)
        Path("started.txt").write_text("descendants launched")
        print("descendants-started", flush=True)
        while True:
            time.sleep(1)
    boundary()
    if kind == "database":
        # -I keeps this fixed probe independent of candidate Python modules.
        sys.path.insert(0, "/opt/executor")
        from vibearr_executor.pg_smoke import query

        context = json.loads(Path(os.environ["VIBEARR_CONTEXT"]).read_bytes())
        if context.get("foreign_db"):
            denied(context["foreign_db"], 5432)
        address = query(os.environ["DATABASE_URL"])
        Path("db-proof.txt").write_text(
            "fresh database query returned 42 at " + address
        )
        time.sleep(context.get("hold_seconds", 0))
        print("fresh-disposable-postgresql-query-passed", flush=True)
        network()
    elif kind == "limits":
        limits()
    else:
        network(install=kind == "setup", model=kind == "agent")
    if kind in {"agent", "leak"}:
        secret = os.environ["VIBEARR_SMOKE_KEY"]
        check(
            Path(os.environ["VIBEARR_BACKEND_SECRET_FILE"]).read_text() == secret,
            "Selected credential file is unavailable",
        )
        print(secret, flush=True)
        if kind == "leak":
            Path("leaked-key.txt").write_text(secret)
    else:
        check(
            "VIBEARR_BACKEND_SECRET_FILE" not in os.environ
            and "VIBEARR_SMOKE_KEY" not in os.environ,
            "Offline/setup phase received backend secret",
        )
    print("qualification-recipe-passed", flush=True)


if __name__ == "__main__":
    main()
