"""Supervise the web server and durable import scheduler together."""

from contextlib import contextmanager
import os
from pathlib import Path
import signal
import subprocess
import threading
import time


STARTUP_LOCK = 839150276410
HEARTBEAT = Path("/tmp/vibearr-scheduler-heartbeat")


def signal_group(process, signum):
    try:
        os.killpg(process.pid, signum)
    except ProcessLookupError:
        pass


def group_running(process):
    for directory in Path("/proc").iterdir():
        if not directory.name.isdigit():
            continue
        try:
            fields = (directory / "stat").read_text().rsplit(")", 1)[1].split()
        except (FileNotFoundError, ProcessLookupError):
            continue
        if int(fields[2]) == process.pid and fields[0] != "Z":
            return True
    return False


def wait_groups(processes, timeout):
    deadline = time.monotonic() + timeout
    while True:
        for process in processes:
            process.poll()
        if not any(group_running(process) for process in processes):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.02)


def stop_processes(processes, timeout=20):
    for process in processes:
        signal_group(process, signal.SIGTERM)
    if not wait_groups(processes, timeout):
        for process in processes:
            signal_group(process, signal.SIGKILL)
        if not wait_groups(processes, 5):
            raise RuntimeError("Application child termination is unconfirmed.")
    for process in processes:
        process.wait(timeout=0.1)


def startup_command(command, stop, timeout=600):
    if stop.is_set():
        return False
    process = subprocess.Popen(command, start_new_session=True)
    deadline = time.monotonic() + timeout
    try:
        while process.poll() is None:
            if stop.wait(0.1):
                return False
            if time.monotonic() >= deadline:
                raise RuntimeError("Application startup command timed out.")
        if process.returncode:
            raise subprocess.CalledProcessError(process.returncode, command)
        return not stop.is_set()
    finally:
        stop_processes([process])


@contextmanager
def database_startup_lock(connection, stop, timeout=120):
    deadline = time.monotonic() + timeout
    acquired = False
    try:
        while not stop.is_set():
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_try_advisory_lock(%s)", [STARTUP_LOCK])
                acquired = cursor.fetchone()[0]
            if acquired:
                break
            if time.monotonic() >= deadline:
                raise RuntimeError("Application initialization lock timed out.")
            stop.wait(0.1)
        yield acquired
    finally:
        try:
            if acquired:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT pg_advisory_unlock(%s)", [STARTUP_LOCK])
        finally:
            connection.close()


def supervise(commands, stop, shutdown_timeout=20):
    processes = []
    try:
        for command in commands:
            if stop.is_set():
                return 0
            processes.append(subprocess.Popen(command, start_new_session=True))
        while not stop.wait(0.1):
            if any(process.poll() is not None for process in processes):
                return 1
    finally:
        stop_processes(processes, shutdown_timeout)
    return 0


def main():
    stop = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stop.set())
    HEARTBEAT.unlink(missing_ok=True)
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "vibearr_app.settings")
    import django
    from django.db import OperationalError, connection

    django.setup()
    deadline = time.monotonic() + 120
    while not stop.is_set():
        try:
            connection.ensure_connection()
            break
        except OperationalError:
            connection.close()
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    "Application database did not become ready."
                ) from None
            stop.wait(1)
    if stop.is_set():
        connection.close()
        return 0
    with database_startup_lock(connection, stop) as acquired:
        if not acquired:
            return 0
        if not startup_command(["python", "manage.py", "migrate", "--noinput"], stop):
            return 0
        command = [
            "python",
            "manage.py",
            "docker_bootstrap_runner",
            os.environ.get("VIBEARR_RUNNER_NAME", "docker-runner"),
        ]
        if not startup_command(command, stop):
            return 0
    commands = [
        [
            "gunicorn",
            "vibearr_app.wsgi:application",
            "--bind",
            "0.0.0.0:8000",
            "--workers",
            "2",
            "--timeout",
            "90",
            "--access-logfile",
            "-",
            "--error-logfile",
            "-",
        ],
        ["python", "manage.py", "run_scheduler"],
    ]
    return supervise(commands, stop)


if __name__ == "__main__":
    raise SystemExit(main())
