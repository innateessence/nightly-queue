"""Disposable PostgreSQL inside the runner, independent of production."""

import os
from pathlib import Path
import secrets
import shutil
import subprocess


BIN = Path("/usr/lib/postgresql/15/bin")
UID = 10002


class LocalDatabase:
    host = "127.0.0.1"

    def __init__(self, root=Path("/test-database")):
        self.root = root
        self.started = False
        self.password = ""

    def command(self, *arguments):
        def identity():
            os.setgroups([])
            os.setgid(UID)
            os.setuid(UID)

        return subprocess.run(
            [str(BIN / arguments[0]), *arguments[1:]],
            cwd="/",
            preexec_fn=identity,
            env={
                "PATH": str(BIN) + ":/usr/bin:/bin",
                "HOME": str(self.root),
                "PGPASSWORD": self.password,
            },
            check=True,
            capture_output=True,
            timeout=30,
        )

    def reset(self, _operation_id):
        if self.started:
            self.command(
                "pg_ctl", "-D", str(self.root / "data"), "-m", "immediate", "-w", "stop"
            )
            self.started = False
        for path in self.root.iterdir():
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            else:
                path.unlink()

    def prepare(self, operation_id):
        self.reset(operation_id)
        os.chown(self.root, UID, UID)
        self.root.chmod(0o700)
        password = secrets.token_hex(24)
        self.password = password
        password_file = self.root / "password"
        password_file.write_text(password)
        os.chown(password_file, UID, UID)
        password_file.chmod(0o600)
        data = str(self.root / "data")
        self.command(
            "initdb",
            "-D",
            data,
            "-U",
            "task",
            "--pwfile",
            str(password_file),
            "--auth",
            "scram-sha-256",
            "--no-locale",
            "--encoding",
            "UTF8",
        )
        self.command(
            "pg_ctl",
            "-D",
            data,
            "-l",
            str(self.root / "server.log"),
            "-o",
            "-h 127.0.0.1 -p 5432 -k /test-database -c max_connections=40 -c shared_buffers=32MB",
            "-w",
            "start",
        )
        self.started = True
        self.command("createdb", "-h", str(self.root), "-U", "task", "task")
        password_file.unlink()
        return f"postgresql://task:{password}@127.0.0.1:5432/task"
