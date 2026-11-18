"""Supervise the web server and durable import scheduler together."""

import os
import signal
import subprocess
import threading
import time


def main():
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "vibearr_app.settings")
    import django
    from django.db import OperationalError, connection

    django.setup()
    deadline = time.monotonic() + 120
    while True:
        try:
            connection.ensure_connection()
            connection.close()
            break
        except OperationalError:
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    "Application database did not become ready."
                ) from None
            time.sleep(1)
    subprocess.run(["python", "manage.py", "migrate", "--noinput"], check=True)
    subprocess.run(
        [
            "python",
            "manage.py",
            "docker_bootstrap_runner",
            os.environ.get("VIBEARR_RUNNER_NAME", "docker-runner"),
        ],
        check=True,
    )
    stop = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stop.set())
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
    processes = []
    try:
        for command in commands:
            processes.append(subprocess.Popen(command, start_new_session=True))
        while not stop.wait(0.5):
            if any(process.poll() is not None for process in processes):
                return 1
    finally:
        for process in processes:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
        deadline = time.monotonic() + 20
        for process in processes:
            try:
                process.wait(timeout=max(0.1, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
