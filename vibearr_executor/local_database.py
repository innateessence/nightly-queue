"""A phase-local database with private administration and bounded task access."""

import hashlib
import json
import os
from pathlib import Path
import pwd
import secrets
import shutil
import signal
import subprocess
import sys
import time


BIN = Path("/usr/lib/postgresql/15/bin")
UID = 10002
PROCESS_INSPECTOR = """
import json
import os
from pathlib import Path
import sys

data = os.path.realpath(sys.argv[1])
binary = os.path.realpath(sys.argv[2])
uid = sys.argv[3]
result = []
for path in Path('/proc').iterdir():
    if not path.name.isdecimal() or int(path.name) == os.getpid():
        continue
    try:
        fields = path.joinpath('stat').read_text().rsplit(')', 1)[1].split()
        status = path.joinpath('status').read_text().splitlines()
        owner = next(line.split()[1] for line in status if line.startswith('Uid:'))
        if owner != uid or fields[0] == 'Z':
            continue
        executable = os.readlink(path / 'exe')
        directory = os.readlink(path / 'cwd').removesuffix(' (deleted)')
        arguments = path.joinpath('cmdline').read_bytes().split(b'\\0')
        startup = any(
            arguments[index] == b'-D'
            and os.path.realpath(os.fsdecode(arguments[index + 1])) == data
            for index in range(len(arguments) - 1)
        )
        if directory != data and not startup:
            continue
        if executable != binary:
            raise RuntimeError('Private database process has an unknown executable')
        current = path.joinpath('stat').read_text().rsplit(')', 1)[1].split()
        if current[19] != fields[19]:
            continue
        result.append({'pid': int(path.name), 'start_time': int(fields[19])})
    except (FileNotFoundError, ProcessLookupError):
        continue
print(json.dumps(result))
"""


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

    def database_uid_present(self):
        for path in Path("/proc").iterdir():
            if not path.name.isdecimal():
                continue
            try:
                lines = path.joinpath("status").read_text().splitlines()
            except (FileNotFoundError, ProcessLookupError):
                continue
            owner = next(line.split()[1] for line in lines if line.startswith("Uid:"))
            if owner == str(UID):
                return True
        return False

    def owned_processes(self):
        if not self.database_uid_present():
            return []
        result = subprocess.run(
            [
                sys.executable,
                "-I",
                "-c",
                PROCESS_INSPECTOR,
                str(self.data),
                str(self.binary_directory / "postgres"),
                str(UID),
            ],
            cwd="/",
            user=UID,
            group=UID,
            extra_groups=[],
            env=self.environment(),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=True,
            timeout=3,
        )
        if len(result.stdout) > 65536:
            raise LocalDatabaseError(
                "Local database process inventory exceeds its bound."
            )
        records = json.loads(result.stdout)
        if not isinstance(records, list) or len(records) > 1024:
            raise LocalDatabaseError("Local database process inventory is invalid.")
        for record in records:
            if not isinstance(record, dict) or set(record) != {"pid", "start_time"}:
                raise LocalDatabaseError("Local database process identity is invalid.")
            if any(type(value) is not int or value <= 1 for value in record.values()):
                raise LocalDatabaseError("Local database process identity is invalid.")
        return records

    def current_identity(self, pid):
        path = Path("/proc") / str(pid)
        try:
            fields = path.joinpath("stat").read_text().rsplit(")", 1)[1].split()
            lines = path.joinpath("status").read_text().splitlines()
        except (FileNotFoundError, ProcessLookupError):
            return None
        owner = next(line.split()[1] for line in lines if line.startswith("Uid:"))
        return {"pid": pid, "start_time": int(fields[19])}, owner

    def kill_owned_process(self, record):
        try:
            descriptor = os.pidfd_open(record["pid"])
        except ProcessLookupError:
            return
        try:
            current = self.current_identity(record["pid"])
            if current is None or current[0] != record:
                return
            if current[1] != str(UID):
                raise LocalDatabaseError("Local database process ownership changed.")
            try:
                signal.pidfd_send_signal(descriptor, signal.SIGKILL)
            except ProcessLookupError:
                pass
        finally:
            os.close(descriptor)

    def reap_owned_processes(self, records):
        if self.process is not None:
            self.process.poll()
        for record in records.values():
            current = self.current_identity(record["pid"])
            if current is None or current != (record, str(UID)):
                continue
            try:
                os.waitpid(record["pid"], os.WNOHANG)
            except ChildProcessError:
                pass

    def terminate_owned_processes(self):
        deadline = time.monotonic() + 10
        records = {}
        while True:
            if time.monotonic() >= deadline:
                raise LocalDatabaseError(
                    "Local database processes did not terminate; keep the worker fenced."
                )
            current = self.owned_processes()
            self.reap_owned_processes(records)
            if not current:
                if not self.owned_processes():
                    return
                continue
            for record in current:
                records[record["pid"]] = record
                self.kill_owned_process(record)
            time.sleep(0.05)

    def reset(self, _operation_id):
        self.fenced = True
        try:
            self.postmaster()
            self.terminate_owned_processes()
            if self.process is not None:
                self.process.wait(timeout=1)
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
