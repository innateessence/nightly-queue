import fcntl
import json
import ipaddress
import os
import secrets
import socket
import threading
import time
from pathlib import Path
from uuid import uuid4

from .artifacts import result_manifest, unpack
from .contracts import (
    MAX_ARCHIVE,
    MAX_CONTEXT,
    MAX_LOG,
    MAX_RESULT,
    Conflict,
    Manifest,
    Profile,
    Rejected,
    canonical,
    digest,
    identifier,
    sha256,
)
from .engine import Docker, EngineError


def durable(path: Path, content: bytes, mode: int = 0o600) -> None:
    temporary = path.with_suffix(".pending")
    descriptor = os.open(
        temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, mode
    )
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.chmod(mode)
    temporary.replace(path)
    descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class Runtime:
    def __init__(
        self,
        directory: Path,
        profiles: list[Profile],
        *,
        docker: Docker | None = None,
        denied_ips: tuple[str, ...] = (),
        max_active_runs: int = 1,
    ):
        self.configured_ips = tuple(
            str(ipaddress.IPv4Address(value)) for value in denied_ips
        )
        self.directory = directory.resolve()
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.directory.stat().st_mode & 0o077:
            raise Rejected("Executor storage must be private (mode 0700).")
        self.lockfile = (self.directory / "executor.lock").open("a")
        try:
            fcntl.flock(self.lockfile, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.lockfile.close()
            raise
        self.lock = threading.RLock()
        self.docker = docker or Docker()
        self.profiles = {profile.profile_id: profile for profile in profiles}
        if not profiles or len(self.profiles) != len(profiles):
            raise Rejected("Register at least one unique immutable profile.")
        self.denied_ips = self.current_denied_ips()
        self.max_active_runs = max_active_runs
        self.artifacts = self.directory / "artifacts"
        self.operations = self.directory / "operations"
        self.artifacts.mkdir(mode=0o700, exist_ok=True)
        self.operations.mkdir(mode=0o700, exist_ok=True)
        identity = self.directory / "instance"
        if not identity.exists():
            durable(identity, str(uuid4()).encode())
        self.instance = identifier(identity.read_text())

    def close(self):
        self.lockfile.close()

    @staticmethod
    def host_addresses() -> set[str]:
        addresses = {"127.0.0.1"}
        try:
            addresses.update(
                entry[4][0]
                for entry in socket.getaddrinfo(
                    socket.gethostname(), None, socket.AF_INET
                )
            )
        except OSError:
            pass
        try:
            previous = ""
            for line in Path("/proc/net/fib_trie").read_text().splitlines():
                if "/32 host LOCAL" in line:
                    addresses.add(str(ipaddress.IPv4Address(previous.split()[-1])))
                previous = line
        except (OSError, ValueError, IndexError):
            pass
        return addresses

    def current_denied_ips(self) -> tuple[str, ...]:
        return tuple(sorted(set(self.configured_ips) | self.host_addresses()))

    def path(self, operation_id: str) -> Path:
        return self.operations / identifier(operation_id)

    def record(self, operation_id: str) -> dict:
        try:
            return json.loads((self.path(operation_id) / "record.json").read_bytes())
        except FileNotFoundError:
            return {"state": "not-started", "operation_id": operation_id}

    def save(self, record: dict) -> None:
        durable(self.path(record["operation_id"]) / "record.json", canonical(record))

    def upload(self, kind: str, content: bytes, expected_digest: str) -> dict:
        sha256(expected_digest)
        maximum = MAX_ARCHIVE if kind == "source" else MAX_CONTEXT
        if (
            kind not in {"source", "context"}
            or len(content) > maximum
            or digest(content) != expected_digest
        ):
            raise Rejected("Artifact kind, size, or digest does not match.")
        if kind == "source":
            unpack(content)
        with self.lock:
            path = self.artifacts / expected_digest
            if not path.exists():
                usage = sum(file.stat().st_size for file in self.artifacts.iterdir())
                storage = os.statvfs(self.directory)
                if (
                    usage + len(content) > 512 * 1024 * 1024
                    or storage.f_bavail * storage.f_frsize
                    < len(content) + 128 * 1024 * 1024
                ):
                    raise Rejected(
                        "Executor artifact capacity is exhausted; retain active work and clean finished artifacts."
                    )
                durable(path, content)
        return {
            "handle": expected_digest,
            "sha256": expected_digest,
            "bytes": len(content),
        }

    def prepare(self, data: dict) -> dict:
        operation_id = identifier(data.get("operation_id"))
        with self.lock:
            record = self.record(operation_id)
            if "manifest" in record:
                if canonical(record["manifest"]) != canonical(data):
                    raise Conflict(
                        "Operation identity already binds a different manifest."
                    )
                return self.public(record)
            profile = self.profiles.get(data.get("profile_id", ""))
            if profile is None:
                raise Rejected("Runtime profile is not registered.")
            manifest = Manifest.parse(data, profile)
            for key in ("source_digest", "context_digest"):
                path = self.artifacts / data[key]
                if not path.is_file() or digest(path.read_bytes()) != data[key]:
                    raise Rejected(
                        "Upload the exact source/context artifacts before prepare."
                    )
            source = (self.artifacts / manifest.source_digest).read_bytes()
            unpack(source)
            if (self.artifacts / manifest.context_digest).stat().st_size > MAX_CONTEXT:
                raise Rejected("Context artifact exceeds its limit.")
            if len(list(self.operations.iterdir())) >= 1000:
                raise Rejected(
                    "Operation journal is full; retain active work and archive completed operations."
                )
            path = self.path(operation_id)
            storage = os.statvfs(self.directory)
            usage = sum(
                file.stat().st_size
                for file in self.operations.rglob("*")
                if file.is_file()
            )
            if (
                usage > 2 * 1024 * 1024 * 1024
                or storage.f_bavail * storage.f_frsize < 128 * 1024 * 1024
            ):
                raise Rejected(
                    "Executor journal capacity is exhausted; retain active data and archive completed operations."
                )
            path.mkdir(mode=0o700)
            inputs = path / "input"
            inputs.mkdir(mode=0o755)
            durable(inputs / "source.tar", source, 0o444)
            durable(
                inputs / "context",
                (self.artifacts / manifest.context_digest).read_bytes(),
                0o444,
            )
            durable(inputs / "secret", b"")
            record = {
                "operation_id": operation_id,
                "manifest": manifest.data(),
                "profile": profile.data(),
                "state": "not-started",
                "name": f"vibearr-exec-{self.instance[:8]}-{operation_id}",
                "launch_intent": False,
                "gate_intent": False,
            }
            self.save(record)
            return self.public(record)

    def supply_secret(self, operation_id: str, secret: str) -> dict:
        if (
            not isinstance(secret, str)
            or not 16 <= len(secret.encode()) <= 16384
            or "\x00" in secret
        ):
            raise Rejected("Invalid backend credential material.")
        with self.lock:
            record = self.record(operation_id)
            if not record.get("profile", {}).get("backend_secret_env"):
                raise Rejected("This profile cannot receive backend credentials.")
            secret_digest = digest(secret.encode())
            if record.get("secret_digest"):
                if record["secret_digest"] != secret_digest:
                    raise Conflict(
                        "Credential material cannot change within an operation."
                    )
                return {"stored": True}
            if record["launch_intent"]:
                raise Conflict("Credential delivery must precede launch.")
            durable(self.path(operation_id) / "input" / "secret", secret.encode())
            record["secret_digest"] = secret_digest
            self.save(record)
            return {"stored": True}

    def labels(self, record: dict) -> list[str]:
        return [
            "--label",
            f"vibearr.executor={self.instance}",
            "--label",
            f"vibearr.operation={record['operation_id']}",
        ]

    def owned(self, record: dict, name: str) -> dict | None:
        inspected = self.docker.inspect(name)
        if inspected is not None:
            labels = inspected["Config"].get("Labels", {})
            if (
                labels.get("vibearr.executor") != self.instance
                or labels.get("vibearr.operation") != record["operation_id"]
            ):
                raise Conflict(
                    "Container identity is occupied by an unrelated resource."
                )
        return inspected

    def network(self, record: dict, suffix: str, *, internal: bool) -> str:
        name = record["name"] + suffix
        try:
            inspected = json.loads(self.docker.run("network", "inspect", name))[0]
            if (
                inspected.get("Labels", {}).get("vibearr.executor") != self.instance
                or bool(inspected.get("Internal")) != internal
            ):
                raise Conflict("Network identity does not match this executor.")
        except EngineError as exc:
            if "not found" not in str(exc).lower() and "No such" not in str(exc):
                raise
            arguments = ["network", "create", *self.labels(record)]
            if internal:
                arguments.append("--internal")
            self.docker.run(*arguments, name)
        return name

    def create(self, record: dict, name: str, arguments: list[str]) -> dict:
        inspected = self.owned(record, name)
        if inspected is None:
            logging = [
                "--log-driver",
                "local",
                "--log-opt",
                "max-size=1m",
                "--log-opt",
                "max-file=1",
                "--log-opt",
                "compress=false",
            ]
            self.docker.run(
                "create", "--name", name, *self.labels(record), *logging, *arguments
            )
            inspected = self.owned(record, name)
        if inspected is None:
            raise EngineError(
                "Container creation is ambiguous; preserve launch intent."
            )
        return inspected

    def launch(self, record: dict, name: str) -> dict:
        inspected = self.owned(record, name)
        if inspected is None:
            raise EngineError("Recorded container is missing; outcome is unknown.")
        if inspected["State"]["Status"] == "created":
            self.docker.run("start", name)
            inspected = self.owned(record, name)
        if inspected is None or not inspected["State"]["Running"]:
            raise EngineError("Recorded container exited; it cannot be relaunched.")
        return inspected

    @staticmethod
    def address(inspected: dict, network: str) -> str:
        return inspected["NetworkSettings"]["Networks"][network]["IPAddress"]

    def infrastructure(self, record: dict) -> tuple[str, list[str], dict]:
        network = self.network(record, "-net", internal=True)
        destinations: list[str] = []
        job = {"manifest": record["manifest"], "profile": record["profile"]}
        profile = record["profile"]
        path = self.path(record["operation_id"])
        if record["manifest"]["network_hosts"]:
            outside = self.network(record, "-out", internal=False)
            policy = {
                "hosts": record["manifest"]["network_hosts"],
                "denied": self.current_denied_ips(),
            }
            durable(path / "proxy.json", canonical(policy), 0o444)
            proxy = record["name"] + "-proxy"
            arguments = [
                "--network",
                network,
                "--read-only",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges",
                "--memory",
                "128m",
                "--memory-swap",
                "128m",
                "--pids-limit",
                "64",
                "--cpus",
                "1",
                "--user",
                "1000:1000",
                "--mount",
                f"type=bind,src={path / 'proxy.json'},dst=/input/proxy.json,readonly",
                "--entrypoint",
                "python",
                profile["image"],
                "-I",
                "-m",
                "vibearr_executor.proxy",
                "/input/proxy.json",
            ]
            inspected = self.create(record, proxy, arguments)
            if outside not in inspected["NetworkSettings"]["Networks"]:
                self.docker.run("network", "connect", outside, proxy)
            inspected = self.launch(record, proxy)
            address = self.address(inspected, network)
            destinations.append(address + ":3128")
            job["proxy"] = f"http://{address}:3128"
        if profile["database_image"]:
            if not record.get("database_password"):
                record["database_password"] = secrets.token_hex(24)
                self.save(record)
            database = record["name"] + "-db"
            password = record["database_password"]
            database_env = path / "database.env"
            durable(
                database_env,
                f"POSTGRES_USER=task\nPOSTGRES_DB=task\nPOSTGRES_PASSWORD={password}\nPGDATA=/var/lib/postgresql/data/db\n".encode(),
            )
            arguments = [
                "--network",
                network,
                "--read-only",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges",
                "--user",
                "999:999",
                "--memory",
                "256m",
                "--memory-swap",
                "256m",
                "--cpus",
                "1",
                "--pids-limit",
                "64",
                "--tmpfs",
                "/var/lib/postgresql/data:rw,nosuid,nodev,size=128m,uid=999,gid=999,mode=0700",
                "--tmpfs",
                "/var/run/postgresql:rw,nosuid,nodev,size=1m,uid=999,gid=999",
                "--tmpfs",
                "/tmp:rw,nosuid,nodev,size=8m",
                "--env-file",
                str(database_env),
                profile["database_image"],
            ]
            self.create(record, database, arguments)
            inspected = self.launch(record, database)
            ready_deadline = time.monotonic() + 30
            while True:
                try:
                    self.docker.run(
                        "exec",
                        database,
                        "pg_isready",
                        "-U",
                        "task",
                        "-d",
                        "task",
                        timeout=5,
                    )
                    break
                except EngineError:
                    if time.monotonic() >= ready_deadline:
                        raise EngineError(
                            "Disposable PostgreSQL did not become ready."
                        ) from None
                    time.sleep(0.2)
            address = self.address(inspected, network)
            destinations.append(address + ":5432")
            job["database_url"] = f"postgresql://task:{password}@{address}:5432/task"
        return network, destinations, job

    def start(self, operation_id: str) -> dict:
        with self.lock:
            record = self.record(operation_id)
            if "manifest" not in record:
                raise Rejected("Prepare the operation first.")
            if record["state"] == "exited":
                return self.public(record)
            from .qualification import capabilities
            from .qualify import smoke_profiles

            profile = record["profile"]
            probes = smoke_profiles(profile["image"], profile["database_image"])
            is_probe = any(candidate.data() == profile for candidate in probes)
            if not is_probe and not capabilities(self)["qualified"]:
                raise Rejected(
                    "Installed runtime/profile qualification is missing or stale. Run automated boundary verification first."
                )
            active_runs = set()
            for path in self.operations.iterdir():
                other = self.record(path.name)
                if other.get("launch_intent") and other["state"] != "exited":
                    active_runs.add(other["manifest"]["run_id"])
            if (
                record["manifest"]["run_id"] not in active_runs
                and len(active_runs) >= self.max_active_runs
            ):
                raise Conflict(
                    "An active or unknown run holds the executor fence; reconcile/stop it first."
                )
            if record.get("launch_intent"):
                existing = self.owned(record, record["name"])
                if existing is None and record.get("container_id"):
                    return self.status(operation_id)
                if record.get("gate_intent"):
                    if existing is not None and existing["State"]["Running"]:
                        self.docker.run("exec", record["name"], "touch", "/state/go")
                    return self.status(operation_id)
            if record["profile"]["backend_secret_env"] and not record.get(
                "secret_digest"
            ):
                raise Rejected(
                    "Deliver the selected backend credential before agent launch."
                )
            if (
                time.time()
                >= __import__("datetime")
                .datetime.fromisoformat(record["manifest"]["deadline"])
                .timestamp()
            ):
                raise Rejected("Job deadline expired before launch.")
            record["launch_intent"] = True
            self.save(record)
            network, destinations, job = self.infrastructure(record)
            inputs = self.path(operation_id) / "input"
            durable(inputs / "job.json", canonical(job))
            limits = record["manifest"]["limits"]
            arguments = [
                "--network",
                network,
                "--dns",
                "127.0.0.1",
                "--hostname",
                "task",
                "--user",
                "0:0",
                "--read-only",
                "--cap-drop",
                "ALL",
            ]
            for capability in ("CHOWN", "DAC_OVERRIDE", "KILL", "SETUID", "SETGID"):
                arguments.extend(("--cap-add", capability))
            arguments.extend(
                [
                    "--security-opt",
                    "no-new-privileges",
                    "--memory",
                    f"{limits['memory_mb']}m",
                    "--memory-swap",
                    f"{limits['memory_mb']}m",
                    "--pids-limit",
                    str(limits["pids"]),
                    "--cpus",
                    str(limits["cpu"]),
                    "--ulimit",
                    "nofile=1024:1024",
                    "--ulimit",
                    "core=0",
                    "--shm-size",
                    "8m",
                ]
            )
            mounts = {
                "/work": f"size={limits['workspace_mb']}m,uid=1000,gid=1000,mode=0700",
                "/state": "size=48m,mode=0700,noexec",
                "/secrets": "size=64k,mode=0711,noexec",
                "/tmp": "size=16m,mode=1777",
                "/home/task": "size=32m,uid=1000,gid=1000,mode=0700",
            }
            for destination, options in mounts.items():
                arguments.extend(
                    ("--tmpfs", f"{destination}:rw,nosuid,nodev,{options}")
                )
            arguments.extend(
                (
                    "--mount",
                    f"type=bind,src={inputs},dst=/input,readonly",
                    "--entrypoint",
                    "python",
                    record["profile"]["image"],
                    "-I",
                    "-m",
                    "vibearr_executor.worker",
                )
            )
            inspected = self.create(record, record["name"], arguments)
            record["container_id"] = inspected["Id"]
            self.save(record)
            self.launch(record, record["name"])
            boot_deadline = time.monotonic() + 30
            while self.docker.read(record["name"], "booted", 1024) != b"ready":
                if time.monotonic() >= boot_deadline:
                    raise EngineError(
                        "Trusted runtime did not initialize; launch gate remains closed."
                    )
                time.sleep(0.1)
            durable(inputs / "secret", b"")
            guard = record["name"] + "-guard"
            guard_args = [
                "--user",
                "0:0",
                "--network",
                f"container:{record['name']}",
                "--read-only",
                "--cap-drop",
                "ALL",
                "--cap-add",
                "NET_ADMIN",
                "--security-opt",
                "no-new-privileges",
                "--memory",
                "64m",
                "--memory-swap",
                "64m",
                "--pids-limit",
                "16",
                "--cpus",
                "1",
                "--entrypoint",
                "python",
                record["profile"]["image"],
                "-I",
                "-m",
                "vibearr_executor.firewall",
                *destinations,
            ]
            inspected = self.create(record, guard, guard_args)
            if inspected["State"]["Status"] == "created":
                self.docker.run("start", "-a", guard)
            guard_state = self.owned(record, guard)
            if (
                guard_state is None
                or guard_state["State"]["Running"]
                or guard_state["State"]["ExitCode"] != 0
            ):
                raise EngineError(
                    "Network policy installation failed; task launch gate remains closed."
                )
            record["gate_intent"] = True
            record["state"] = "running"
            self.save(record)
            self.docker.run("exec", record["name"], "touch", "/state/go")
            return self.status(operation_id)

    def harvest(self, record: dict) -> bool:
        name = record["name"]
        content = self.docker.read(name, "result.json", MAX_RESULT)
        if content is None:
            return False
        result = json.loads(content)
        if (
            result.get("operation_id") != record["operation_id"]
            or result.get("run_id") != record["manifest"]["run_id"]
            or result.get("terminated") is not True
        ):
            raise EngineError(
                "Runtime result identity or termination evidence is invalid."
            )
        path = self.path(record["operation_id"])
        logs = self.docker.read(name, "logs.bin", MAX_LOG)
        if logs is None or digest(logs) != result["log_digest"]:
            raise EngineError("Runtime log digest does not match.")
        durable(path / "logs.bin", logs)
        if result.get("artifact"):
            exported = self.docker.read(name, "source.tar", MAX_ARCHIVE)
            source = (path / "input" / "source.tar").read_bytes()
            if exported is None or canonical(
                result_manifest(source, exported)
            ) != canonical(result["artifact"]):
                raise EngineError("Runtime source export digest does not match.")
            durable(path / "source.tar", exported)
        durable(path / "result.json", content)
        durable(path / "input" / "secret", b"")
        record["result"] = result
        self.save(record)
        return True

    def cleanup(self, record: dict) -> None:
        for suffix in ("", "-guard", "-proxy", "-db"):
            name = record["name"] + suffix
            if self.owned(record, name) is not None:
                self.docker.remove("container", name)
            if self.owned(record, name) is not None:
                raise EngineError("Container termination could not be confirmed.")
        for suffix in ("-net", "-out"):
            self.docker.remove("network", record["name"] + suffix)
        durable(self.path(record["operation_id"]) / "input" / "secret", b"")
        record["state"] = "exited"
        record["descendants_terminated"] = True
        self.save(record)

    def status(self, operation_id: str) -> dict:
        with self.lock:
            record = self.record(operation_id)
            if (
                "manifest" not in record
                or not record.get("launch_intent")
                or record["state"] == "exited"
            ):
                return self.public(record)
            inspected = self.owned(record, record["name"])
            if record.get("result"):
                self.cleanup(record)
            elif inspected is not None and inspected["State"]["Running"]:
                if self.harvest(record):
                    self.cleanup(record)
            else:
                durable(self.path(operation_id) / "input" / "secret", b"")
                record["state"] = "unknown"
                self.save(record)
            return self.public(record)

    def stop(self, operation_id: str) -> dict:
        with self.lock:
            record = self.record(operation_id)
            if "manifest" not in record or record["state"] == "exited":
                return self.public(record)
            record["stop_intent"] = True
            self.save(record)
            inspected = self.owned(record, record["name"])
            if inspected is not None and inspected["State"]["Running"]:
                self.docker.run("kill", "--signal", "TERM", record["name"])
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline:
                    if self.harvest(record):
                        break
                    time.sleep(0.1)
            if not record.get("result"):
                record["result"] = {
                    "operation_id": operation_id,
                    "exit_code": None,
                    "terminated": True,
                    "export_error": "Forced termination; no verified workspace export.",
                }
                durable(
                    self.path(operation_id) / "result.json", canonical(record["result"])
                )
                self.save(record)
            self.cleanup(record)
            return self.public(record)

    @staticmethod
    def public(record: dict) -> dict:
        keys = (
            "operation_id",
            "state",
            "container_id",
            "descendants_terminated",
            "result",
        )
        return {key: record[key] for key in keys if key in record}

    def logs(self, operation_id: str, offset: int, limit: int) -> bytes:
        if (
            type(offset) is not int
            or type(limit) is not int
            or offset < 0
            or not 1 <= limit <= 65536
        ):
            raise Rejected("Invalid bounded log range.")
        status = self.status(operation_id)
        path = self.path(operation_id) / "logs.bin"
        if not path.exists():
            record = self.record(operation_id)
            if status["state"] != "running":
                return b""
            content = self.docker.read(record["name"], "logs.bin", MAX_LOG)
            return (content or b"")[offset : offset + limit]
        with path.open("rb") as stream:
            stream.seek(offset)
            return stream.read(limit)

    def export(self, operation_id: str) -> tuple[dict, bytes]:
        status = self.status(operation_id)
        if (
            status.get("state") != "exited"
            or not status.get("descendants_terminated")
            or not status.get("result", {}).get("artifact")
        ):
            raise Conflict("No verified terminated source export is available.")
        return (
            status["result"]["artifact"],
            (self.path(operation_id) / "source.tar").read_bytes(),
        )
