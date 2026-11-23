"""A phase-local database with private administration and bounded task access."""

import hashlib
import os
from pathlib import Path
import pwd
import secrets
import shutil
import signal
import subprocess
import time


BIN = Path("/usr/lib/postgresql/15/bin")
UID = 10002


class LocalDatabaseError(RuntimeError):
    pass


class LocalDatabase:
    host = "127.0.0.1"

    def __init__(
        self,
        root=Path("/test-database"),
        *,
        binary_directory=BIN,
        expected_major=15,
        port=5432,
    ):
        self.root = root
        self.binary_directory = binary_directory
        if not root.is_absolute() or not binary_directory.is_absolute():
            raise ValueError("Local database paths must be trusted absolute paths.")
        if type(expected_major) is not int or expected_major < 1:
            raise ValueError("Configure the installed PostgreSQL major version.")
        if type(port) is not int or not 1024 <= port <= 65535:
            raise ValueError("Choose a nonprivileged local test database port.")
        self.expected_major, self.port = expected_major, port
        self.started = False
        self.fenced = False
        self.process = None

    @property
    def data(self):
        return self.root / "data"

    def environment(self):
        return {
            "PATH": str(self.binary_directory) + ":/usr/bin:/bin",
            "HOME": str(self.root),
            "LANG": "C",
        }

    def command(self, *arguments, input=None, check=True, timeout=30):
        argv = [str(self.binary_directory / arguments[0]), *arguments[1:]]
        process = subprocess.Popen(
            argv,
            cwd="/",
            user=UID,
            group=UID,
            extra_groups=[],
            env=self.environment(),
            stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        try:
            output, error = process.communicate(input, timeout=timeout)
        except BaseException:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                process.communicate(timeout=1)
            except Exception:
                self.fenced = True
                raise LocalDatabaseError(
                    "Local database command cleanup is uncertain; keep the worker fenced."
                )
            raise
        if check and process.returncode:
            raise subprocess.CalledProcessError(process.returncode, argv, output, error)
        return subprocess.CompletedProcess(argv, process.returncode, output, error)

    def provenance(self):
        binary = self.binary_directory / "postgres"
        version = self.command("postgres", "--version").stdout.decode().strip()
        prefix = "postgres (PostgreSQL) "
        major = version.removeprefix(prefix).split(".")[0]
        if not version.startswith(prefix) or major != str(self.expected_major):
            raise LocalDatabaseError(
                "Installed local PostgreSQL version differs from its trusted profile."
            )
        return {
            "postgresql_version": version,
            "postgresql_major": self.expected_major,
            "postgres_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
            "binary_directory": str(self.binary_directory),
        }

    def process_identity(self, pid):
        process = Path("/proc") / str(pid)
        try:
            status = process.joinpath("status").read_text().splitlines()
            user = next(line.split()[1] for line in status if line.startswith("Uid:"))
            state = next(
                line.split()[1] for line in status if line.startswith("State:")
            )
            if state == "Z":
                return False
            arguments = process.joinpath("cmdline").read_bytes().split(b"\0")
            directory = [b"-D", os.fsencode(self.data)]
            matches = any(
                arguments[index : index + 2] == directory
                for index in range(len(arguments))
            )
            if (
                user != str(UID)
                or arguments[0] != os.fsencode(self.binary_directory / "postgres")
                or not matches
            ):
                raise LocalDatabaseError(
                    "Local database process identity is uncertain; keep the worker fenced."
                )
            return True
        except FileNotFoundError:
            return False

    def postmaster(self):
        pidfile = self.data / "postmaster.pid"
        if pidfile.exists():
            try:
                pid = int(pidfile.read_text().splitlines()[0])
            except (ValueError, IndexError) as exc:
                raise LocalDatabaseError(
                    "Local database PID state is invalid; keep the worker fenced."
                ) from exc
            if pid <= 1:
                raise LocalDatabaseError(
                    "Local database PID state is invalid; keep the worker fenced."
                )
            if self.process_identity(pid):
                return pid
        if self.process is not None and self.process.poll() is None:
            return self.process.pid
        for path in Path("/proc").iterdir():
            if not path.name.isdecimal():
                continue
            try:
                arguments = path.joinpath("cmdline").read_bytes().split(b"\0")
            except FileNotFoundError:
                continue
            directory = [b"-D", os.fsencode(self.data)]
            if not any(
                arguments[index : index + 2] == directory
                for index in range(len(arguments))
            ):
                continue
            if self.process_identity(int(path.name)):
                return int(path.name)
        return None

    def reset(self, _operation_id):
        try:
            pid = self.postmaster()
            if pid is not None:
                if (self.data / "postmaster.pid").exists():
                    self.command(
                        "pg_ctl",
                        "-D",
                        str(self.data),
                        "-m",
                        "immediate",
                        "-w",
                        "-t",
                        "10",
                        "stop",
                        timeout=15,
                    )
                else:
                    os.kill(pid, signal.SIGTERM)
                    if self.process is not None and self.process.pid == pid:
                        self.process.wait(timeout=10)
                deadline = time.monotonic() + 10
                while self.process_identity(pid):
                    if time.monotonic() >= deadline:
                        raise LocalDatabaseError(
                            "Local database did not terminate; keep the worker fenced."
                        )
                    time.sleep(0.05)
            if self.process is not None:
                self.process.wait(timeout=1)
            elif pid is not None:
                try:
                    os.waitpid(pid, os.WNOHANG)
                except ChildProcessError:
                    pass
            self.process = None
            self.started = False
            if self.root.exists():
                for path in self.root.iterdir():
                    if path.is_dir() and not path.is_symlink():
                        shutil.rmtree(path)
                    else:
                        path.unlink()
            self.fenced = False
        except Exception:
            self.fenced = True
            raise

    def sql(self, statement):
        admin = pwd.getpwuid(UID).pw_name
        return self.command(
            "psql",
            "-X",
            "-v",
            "ON_ERROR_STOP=1",
            "-h",
            str(self.root),
            "-p",
            str(self.port),
            "-U",
            admin,
            "-d",
            "postgres",
            "--no-password",
            input=statement.encode(),
        )

    def wait_ready(self, cancel):
        deadline = time.monotonic() + 30
        while True:
            if cancel is not None and cancel.is_set():
                raise LocalDatabaseError("Local database startup was canceled.")
            if self.process.poll() is not None:
                raise LocalDatabaseError("Local database exited during startup.")
            ready = self.command(
                "pg_isready",
                "-h",
                str(self.root),
                "-p",
                str(self.port),
                check=False,
                timeout=3,
            )
            if ready.returncode == 0:
                return
            if time.monotonic() >= deadline:
                raise LocalDatabaseError("Local database startup timed out.")
            time.sleep(0.05)

    def prepare(self, operation_id, *, allow_createdb=True, cancel=None):
        if type(allow_createdb) is not bool:
            raise ValueError(
                "Configure test database creation through a trusted profile."
            )
        self.reset(operation_id)
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chown(self.root, os.geteuid(), os.getegid())
        self.root.chmod(0o700)
        os.chown(self.root, UID, UID)
        self.provenance()
        admin = pwd.getpwuid(UID).pw_name
        password = secrets.token_hex(24)
        try:
            self.command(
                "initdb",
                "-D",
                str(self.data),
                "-U",
                admin,
                "--auth-local",
                "peer",
                "--auth-host",
                "scram-sha-256",
                "--no-locale",
                "--encoding",
                "UTF8",
            )
            with (self.root / "server.log").open("wb") as output:
                self.process = subprocess.Popen(
                    [
                        str(self.binary_directory / "postgres"),
                        "-D",
                        str(self.data),
                        "-h",
                        self.host,
                        "-p",
                        str(self.port),
                        "-k",
                        str(self.root),
                        "-c",
                        "max_connections=40",
                        "-c",
                        "shared_buffers=32MB",
                    ],
                    cwd="/",
                    user=UID,
                    group=UID,
                    extra_groups=[],
                    env=self.environment(),
                    stdin=subprocess.DEVNULL,
                    stdout=output,
                    stderr=output,
                    start_new_session=True,
                )
            self.wait_ready(cancel)
            createdb = "CREATEDB" if allow_createdb else "NOCREATEDB"
            options = f"LOGIN NOSUPERUSER NOCREATEROLE NOREPLICATION NOBYPASSRLS INHERIT {createdb} CONNECTION LIMIT 32"
            statement = f"CREATE ROLE task {options} PASSWORD '{password}';\n"
            statement += "CREATE DATABASE task OWNER task;\n"
            self.sql(statement)
            self.started = True
            return f"postgresql://task:{password}@{self.host}:{self.port}/task"
        except Exception:
            self.reset(operation_id)
            raise
