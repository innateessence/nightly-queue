"""Durable jobs in a fixed Compose worker, without engine access."""

import fcntl
import json
import os
from pathlib import Path
import select
import shutil
import signal
import subprocess
import threading
import time
from datetime import datetime
from uuid import uuid4

from .artifacts import extract, result_manifest, snapshot, unpack
from .account_bridge import AccountBridge
from .contracts import (
    MAX_ARCHIVE,
    MAX_CONTEXT,
    MAX_LOG,
    Conflict,
    Manifest,
    NotFound,
    Rejected,
    canonical,
    digest,
    identifier,
    sha256,
)
from .runtime import durable
from .secret_material import contains_secret, protection_material, redact
from .worker import terminate_children


PROTOCOL_VERSION = 2
RUNTIME = "compose-slot-v2"
MAX_ARTIFACT_STORAGE = 512 * 1024**2


class SlotRuntime:
    def __init__(
        self,
        directory,
        profiles,
        *,
        work=Path("/work"),
        home=Path("/home/task"),
        inputs=Path("/input"),
        secrets=Path("/secrets"),
        configure_network=None,
        database=None,
    ):
        self.directory = directory
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory.chmod(0o700)
        self.lockfile = (directory / "worker.lock").open("a")
        fcntl.flock(self.lockfile, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.lock = threading.RLock()
        self.profiles = {value.profile_id: value for value in profiles}
        if not profiles or len(self.profiles) != len(profiles):
            raise Rejected("Register unique image-owned slot profiles.")
        self.operations = directory / "operations"
        self.artifacts = directory / "artifacts"
        for path in (self.operations, self.artifacts):
            path.mkdir(mode=0o700, exist_ok=True)
        instance = directory / "instance"
        if not instance.exists():
            durable(instance, str(uuid4()).encode())
        self.instance = identifier(instance.read_text())
        self.boot = str(uuid4())
        self.work, self.home, self.inputs, self.secrets = work, home, inputs, secrets
        self.configure_network = configure_network or (lambda profile: [])
        self.database = database
        self.thread = None
        self.cancel = threading.Event()
        self.process = None
        self.active = ""
        self.fenced = ""
        self.qualifying = False
        self.account_bridge: AccountBridge | None = None
        from .slot_artifacts import DependencyArtifacts

        self.dependency_artifacts = DependencyArtifacts(self)
        self.probe_operation = ""
        self.probe_profile = None
        self.qualification_lock = threading.Lock()
        terminate_children()
        self.reset()
        for path in self.operations.iterdir():
            record = self.record(path.name)
            if record.get("state") in {"launching", "running", "stopping", "unknown"}:
                record.update(
                    state="exited",
                    descendants_terminated=True,
                    paused=False,
                    result={
                        "schema_version": 1,
                        "run_id": record["manifest"]["run_id"],
                        "operation_id": record["operation_id"],
                        "exit_code": None,
                        "terminated": True,
                        "timed_out": False,
                        "worker_restart": True,
                        "started_at": None,
                        "ended_at": time.time(),
                        "export_error": "Worker restarted; no verified candidate export survived.",
                    },
                )
                if "execution_identity" in record:
                    record["result"]["execution_identity"] = record[
                        "execution_identity"
                    ]
                (path / "backend-secret").unlink(missing_ok=True)
                self.seal(record)

    def reset(self):
        terminate_children()
        for path in (
            self.work,
            self.home,
            self.inputs,
            self.secrets,
            Path("/tmp"),
            Path("/dev/shm"),
            Path("/dev/mqueue"),
            Path("/opt/candidate"),
        ):
            for child in path.iterdir():
                if child.is_symlink() or not child.is_dir():
                    child.unlink()
                else:
                    shutil.rmtree(child)
        subprocess.run(
            ["/usr/local/bin/python", "-I", "-m", "vibearr_executor.slot_ipc_cleanup"],
            cwd="/opt/executor",
            env={"PATH": "/usr/local/bin:/usr/bin:/bin"},
            check=True,
            timeout=10,
        )
        for path in (self.work, self.home):
            path.chmod(0o700)
            os.chown(path, 1000, 1000)
        self.inputs.chmod(0o711)
        self.secrets.chmod(0o711)

    def path(self, operation_id):
        return self.operations / identifier(operation_id)

    def record(self, operation_id):
        path = self.path(operation_id) / "record.json"
        if not path.exists():
            return {"operation_id": operation_id, "state": "not-started"}
        return json.loads(path.read_bytes())

    def save(self, record):
        durable(self.path(record["operation_id"]) / "record.json", canonical(record))

    def execution_identity(self):
        from .slot_safety import binding

        return {
            "runtime": "compose-slot-v2",
            "protocol_version": 2,
            "instance_id": self.instance,
            "worker_boot": self.boot,
            "binding_digest": digest(canonical(binding(self))),
        }

    def seal(self, record):
        record["receipt_digest"] = digest(
            canonical(
                {
                    "manifest": record["manifest"],
                    "result": record["result"],
                    "boot": record["worker_boot"],
                }
            )
        )
        self.save(record)

    @staticmethod
    def public(record):
        fields = {
            "operation_id",
            "state",
            "descendants_terminated",
            "result",
            "paused",
            "receipt_digest",
            "wait_reason",
            "worker_boot",
            "execution_identity",
            "resources_removed",
            "tombstone",
        }
        result = {key: value for key, value in record.items() if key in fields}
        result["state"] = {"prepared": "not-started", "launching": "running"}.get(
            record["state"], record["state"]
        )
        return result

    def upload(self, kind, content, expected_digest):
        sha256(expected_digest)
        maximum = {"source": MAX_ARCHIVE, "context": MAX_CONTEXT}.get(kind, 0)
        if not maximum or len(content) > maximum or digest(content) != expected_digest:
            raise Rejected("Artifact kind, size or digest differs.")
        if kind == "source":
            unpack(content)
        with self.lock:
            path = self.artifacts / expected_digest
            if not path.exists():
                used = sum(path.stat().st_size for path in self.artifacts.iterdir())
                if used + len(content) > MAX_ARTIFACT_STORAGE:
                    raise Rejected("Worker artifact capacity is exhausted.")
                durable(path, content)
        return {
            "handle": expected_digest,
            "sha256": expected_digest,
            "bytes": len(content),
        }

    def prepare(self, data):
        operation_id = identifier(data.get("operation_id"))
        with self.lock:
            record = self.record(operation_id)
            if "manifest" in record:
                if canonical(record["manifest"]) != canonical(data):
                    raise Conflict("Operation already binds a different manifest.")
                return self.public(record)
            profile = self.profile_for(data)
            if profile is None:
                raise Rejected("Profile is not registered on this worker.")
            manifest = Manifest.parse(data, profile)
            if (
                manifest.limits != profile.limits
                or manifest.network_hosts != profile.network_hosts
            ):
                raise Rejected(
                    "Requested limits and egress must match this fixed slot exactly."
                )
            for key in ("source_digest", "context_digest"):
                path = self.artifacts / data[key]
                if not path.is_file() or digest(path.read_bytes()) != data[key]:
                    raise Rejected("Upload the bound artifacts before preparing a job.")
            retained = sum(
                not self.record(path.name).get("tombstone")
                for path in self.operations.iterdir()
            )
            if retained >= 1000:
                raise Rejected("Worker journal capacity is exhausted.")
            self.path(operation_id).mkdir(mode=0o700)
            record = {
                "operation_id": operation_id,
                "manifest": data,
                "state": "prepared",
                "worker_boot": self.boot,
                "execution_identity": self.execution_identity(),
            }
            self.save(record)
            return self.public(record)

    def supply_secret(self, operation_id, secret):
        with self.lock:
            record = self.record(operation_id)
            profile = self.profile_for(record.get("manifest", {}))
            if (
                profile is None
                or profile.phase != "agent"
                or not profile.backend_secret_env
            ):
                raise Rejected(
                    "Only an agent operation may receive a model credential."
                )
            if (
                record["state"] != "prepared"
                or not isinstance(secret, str)
                or not 1 <= len(secret.encode()) <= 16384
            ):
                raise Conflict("Credential requires a prepared agent operation.")
            path = self.path(operation_id) / "backend-secret"
            if path.exists() and path.read_text() != secret:
                raise Conflict("Operation already binds a different credential.")
            if self.account_bridge is not None:
                from .codex_task import ARGV, HOSTS

                if profile.argv == ARGV and profile.network_hosts == HOSTS["session"]:
                    self.account_bridge.bind(record, json.loads(secret))
            durable(path, secret.encode())
            return self.public(record)

    def start(self, operation_id):
        with self.lock:
            record = self.record(operation_id)
            if record["state"] != "prepared":
                if record["state"] == "not-started":
                    raise NotFound("Operation was never prepared.")
                return self.public(record)
            if self.active or self.fenced:
                raise Conflict("Worker slot is busy or fenced for reconciliation.")
            if record["worker_boot"] != self.boot:
                raise Conflict(
                    "Prepared operation belongs to an earlier worker boot; reconcile it before new work."
                )
            if (
                self.qualifying
                and self.probe_operation
                and operation_id != self.probe_operation
            ):
                raise Conflict("Boundary qualification owns the slot.")
            if not self.qualifying and not self.capabilities()["qualified"]:
                raise Rejected("Qualify this Docker worker boot before task execution.")
            profile = self.profile_for(record["manifest"])
            if profile is None:
                raise Rejected("The prepared recipe is no longer installed.")
            manifest = Manifest.parse(record["manifest"], profile)
            secret_path = self.path(operation_id) / "backend-secret"
            if profile.backend_secret_env and not secret_path.exists():
                raise Conflict("Supply the selected model credential before start.")
            self.reset()
            destinations = self.configure_network(profile)
            database_url = (
                self.database.prepare(operation_id)
                if profile.database_image and self.database
                else ""
            )
            if profile.database_image and not database_url:
                raise Rejected("This profile requires an isolated test database slot.")
            extract((self.artifacts / manifest.source_digest).read_bytes(), self.work)
            dependency = None
            if profile.phase == "validation":
                requirements = self.work / "requirements.txt"
                baseline = Path("/opt/validation/project-requirements.txt")
                if (
                    requirements.exists()
                    and baseline.exists()
                    and digest(requirements.read_bytes())
                    != digest(baseline.read_bytes())
                ):
                    dependency = self.dependency_artifacts.install(
                        manifest.run_id, digest(requirements.read_bytes())
                    )
            for parent, directories, names in os.walk(self.work):
                for name in directories + names:
                    os.chown(Path(parent) / name, 1000, 1000)
            context = self.inputs / "context"
            durable(
                self.inputs / "source.tar",
                (self.artifacts / manifest.source_digest).read_bytes(),
                0o444,
            )
            context.write_bytes((self.artifacts / manifest.context_digest).read_bytes())
            context.chmod(0o444)
            secret = secret_path.read_bytes() if secret_path.exists() else b""
            environment = {
                "PATH": "/usr/local/bin:/usr/bin:/bin",
                "HOME": str(self.home),
                "LANG": "C.UTF-8",
                "PYTHONUNBUFFERED": "1",
                "PYTHONDONTWRITEBYTECODE": "1",
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": "/dev/null",
                "VIBEARR_CONTEXT": str(context),
                "VIBEARR_PHASE": profile.phase,
                "TMPDIR": "/tmp",
            }
            if destinations:
                proxy = f"http://{destinations[0][0]}:{destinations[0][1]}"
                environment.update(
                    HTTP_PROXY=proxy,
                    HTTPS_PROXY=proxy,
                    http_proxy=proxy,
                    https_proxy=proxy,
                )
            if database_url:
                environment["DATABASE_URL"] = database_url
                environment["VIBEARR_DISPOSABLE_DATABASE_URL"] = database_url
            if secret:
                target = self.secrets / "backend"
                target.write_bytes(secret)
                target.chmod(0o400)
                os.chown(target, 1000, 1000)
                environment["VIBEARR_BACKEND_SECRET_FILE"] = str(target)
            record["dependency_artifact"] = dependency
            record["execution_identity"] = self.execution_identity()
            record.update(
                state="launching",
                worker_boot=self.boot,
                database_url=database_url,
                paused=False,
            )
            self.active = operation_id
            self.cancel.clear()
            self.save(record)
            self.thread = threading.Thread(
                target=self.run,
                args=(operation_id, profile, environment, secret),
                daemon=True,
            )
            self.thread.start()
            return self.public(record)

    def run(self, operation_id, profile, environment, secret):
        record = self.record(operation_id)
        protected = self.protection(
            operation_id, secret, record.get("database_url", "")
        )
        logs = bytearray()
        result = {
            "schema_version": 1,
            "run_id": record["manifest"]["run_id"],
            "operation_id": operation_id,
            "execution_identity": record["execution_identity"],
            "exit_code": None,
            "terminated": False,
            "timed_out": False,
            "canceled": False,
            "log_truncated": False,
            "started_at": time.time(),
        }
        try:
            deadline = datetime.fromisoformat(
                record["manifest"]["deadline"]
            ).timestamp()
            if self.cancel.is_set():
                raise Rejected("Operation stopped before launch.")
            command = [
                "/usr/local/bin/python",
                "-I",
                "-m",
                "vibearr_executor.slot_child",
                *profile.argv,
            ]
            self.process = subprocess.Popen(
                command,
                cwd="/opt/executor",
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            with self.lock:
                record["state"] = "running"
                self.save(record)
            assert self.process.stdout is not None
            while True:
                if self.cancel.is_set() or time.time() >= deadline:
                    result["timed_out"] = time.time() >= deadline
                    self.process.kill()
                    result["exit_code"] = self.process.wait(timeout=10)
                    terminate_children()
                ready, _, _ = select.select([self.process.stdout], [], [], 0.1)
                if ready:
                    content = os.read(self.process.stdout.fileno(), 65536)
                    logs.extend(content)
                    if len(logs) > MAX_LOG:
                        raise Rejected("Task output exceeds the bounded log limit.")
                    protected = self.protection(
                        operation_id, secret, record.get("database_url", "")
                    )
                    durable(
                        self.path(operation_id) / "logs.bin",
                        redact(bytes(logs), protected, incomplete=True),
                    )
                    if not content and self.process.poll() is not None:
                        break
                if self.process.poll() is not None:
                    terminate_children()
            result["exit_code"] = self.process.wait(timeout=10)
            terminate_children()
            if self.account_bridge is not None:
                self.account_bridge.fence(operation_id)
            protected = self.protection(
                operation_id, secret, record.get("database_url", "")
            )
            result["terminated"] = True
            from .validation_profile import BUILD_ARGV

            if (
                profile.phase == "setup"
                and profile.argv == BUILD_ARGV
                and result["exit_code"] == 0
                and not self.cancel.is_set()
                and not result["timed_out"]
            ):
                from .dependency_artifacts import capture, manifest

                original = (
                    self.artifacts / record["manifest"]["source_digest"]
                ).read_bytes()
                requirements = unpack(original)["requirements.txt"][1]
                content = capture(self.work / ".vibearr-dependencies", requirements)
                durable(self.path(operation_id) / "dependencies.tar", content)
                result["dependency_artifact"] = manifest(content)
                for name in (".vibearr-dependencies", ".vibearr-build-tmp"):
                    shutil.rmtree(self.work / name)
            if record.get("dependency_artifact"):
                result["dependency_artifact"] = record["dependency_artifact"]
            if not self.cancel.is_set() and not result["timed_out"]:
                source = snapshot(self.work)
                if contains_secret(source, protected):
                    raise Rejected(
                        "Candidate export contains selected credential material."
                    )
                original = (
                    self.artifacts / record["manifest"]["source_digest"]
                ).read_bytes()
                result["artifact"] = result_manifest(original, source)
                durable(self.path(operation_id) / "source.tar", source)
        except Exception:
            result["export_error"] = (
                "Worker task failed; inspect bounded redacted diagnostics."
            )
        finally:
            try:
                if self.process is not None and self.process.poll() is None:
                    self.process.kill()
                    result["exit_code"] = self.process.wait(timeout=10)
                terminate_children()
                if self.database and record.get("database_url"):
                    self.database.reset(operation_id)
                self.reset()
                result["terminated"] = True
            except Exception:
                self.fenced = "Termination or reset is unknown; worker remains fenced."
                result["terminated"] = False
            if self.account_bridge is not None:
                self.account_bridge.close(operation_id)
            protected = self.protection(
                operation_id, secret, record.get("database_url", "")
            )
            content = redact(bytes(logs[:MAX_LOG]), protected, incomplete=True)
            durable(self.path(operation_id) / "logs.bin", content)
            result.update(
                ended_at=time.time(),
                canceled=self.cancel.is_set(),
                log_digest=digest(content),
                log_bytes=len(content),
                log_truncated=len(logs) > MAX_LOG,
            )
            (self.path(operation_id) / "backend-secret").unlink(missing_ok=True)
            with self.lock:
                record.pop("database_url", None)
                record.update(
                    state="exited" if result["terminated"] else "unknown",
                    result=result,
                    descendants_terminated=result["terminated"],
                    paused=False,
                )
                self.seal(record)
                self.process = None
                self.active = ""
                if self.account_bridge is not None:
                    self.account_bridge.release(operation_id)

    def protection(self, operation_id, secret, database_url):
        if self.account_bridge is not None:
            return self.account_bridge.protection(operation_id, secret, database_url)
        return protection_material(secret, database_url)

    def status(self, operation_id):
        with self.lock:
            return self.public(self.record(operation_id))

    def stop(self, operation_id):
        with self.lock:
            record = self.record(operation_id)
            if record["state"] == "prepared":
                result = {
                    "schema_version": 1,
                    "run_id": record["manifest"]["run_id"],
                    "operation_id": operation_id,
                    "exit_code": None,
                    "terminated": True,
                    "timed_out": False,
                    "canceled": True,
                    "started_at": None,
                    "ended_at": time.time(),
                    "log_digest": digest(b""),
                    "log_bytes": 0,
                    "log_truncated": False,
                }
                if "execution_identity" in record:
                    result["execution_identity"] = record["execution_identity"]
                record.update(
                    state="exited",
                    descendants_terminated=True,
                    result=result,
                    paused=False,
                )
                (self.path(operation_id) / "backend-secret").unlink(missing_ok=True)
                self.seal(record)
                if self.account_bridge is not None:
                    self.account_bridge.close(operation_id)
                    self.account_bridge.release(operation_id)
            elif operation_id == self.active:
                self.cancel.set()
        if operation_id == self.active and self.thread:
            self.thread.join(timeout=20)
        return self.status(operation_id)

    def pause(self, operation_id):
        from .slot_processes import signal_tasks

        with self.lock:
            record = self.record(operation_id)
            if (
                operation_id != self.active
                or self.process is None
                or record["state"] != "running"
            ):
                raise Conflict("Only a running operation can pause.")
            os.killpg(self.process.pid, signal.SIGSTOP)
            signal_tasks(signal.SIGSTOP)
            record["paused"] = True
            self.save(record)
            return self.public(record)

    def resume(self, operation_id):
        from .slot_processes import signal_tasks

        with self.lock:
            record = self.record(operation_id)
            if (
                operation_id != self.active
                or self.process is None
                or not record.get("paused")
            ):
                raise Conflict("Operation is not paused in this worker boot.")
            os.killpg(self.process.pid, signal.SIGCONT)
            signal_tasks(signal.SIGCONT)
            record["paused"] = False
            self.save(record)
            return self.public(record)

    def logs(self, operation_id, offset, limit):
        from .retention import require_available

        require_available(self.record(operation_id), "logs")
        if (
            type(offset) is not int
            or type(limit) is not int
            or offset < 0
            or not 1 <= limit <= 65536
        ):
            raise Rejected("Invalid bounded log range.")
        path = self.path(operation_id) / "logs.bin"
        if not path.exists():
            return b""
        with path.open("rb") as stream:
            stream.seek(offset)
            return stream.read(limit)

    def export(self, operation_id):
        from .retention import require_available

        record = self.record(operation_id)
        require_available(record, "source")
        if (
            record["state"] != "exited"
            or not record.get("descendants_terminated")
            or not record.get("result", {}).get("artifact")
        ):
            raise Conflict("No terminated verified source export is available.")
        return (
            record["result"]["artifact"],
            (self.path(operation_id) / "source.tar").read_bytes(),
        )

    def termination_receipt(self, operation_id):
        record = self.record(operation_id)
        if "manifest" not in record:
            raise NotFound("Operation was never prepared.")
        result = {
            "schema_version": 1,
            "instance_id": self.instance,
            "operation_id": operation_id,
            "run_id": record["manifest"]["run_id"],
            "manifest_digest": digest(canonical(record["manifest"])),
            "state": record["state"],
            "resources_terminated": record.get("descendants_terminated") is True,
        }
        if result["resources_terminated"]:
            result["receipt_digest"] = record["receipt_digest"]
        return result

    def close(self):
        if self.active:
            self.stop(self.active)
        self.lockfile.close()

    def purge(self, operation_id, run_id, resources):
        from .retention import compact_result, resources as selected_resources

        selected = selected_resources(resources)
        identifier(run_id)
        with self.lock:
            record = self.record(operation_id)
            if record.get("manifest", {}).get("run_id") != run_id:
                raise Conflict("Retention run does not own this operation.")
            if record["state"] != "exited" or not record.get("descendants_terminated"):
                raise Conflict("Only terminated operations may be purged.")
            done = set(record.get("resources_removed", [])) | selected
            record["resources_removed"] = sorted(done)
            self.save(record)
            if "source" in selected:
                (self.path(operation_id) / "source.tar").unlink(missing_ok=True)
                (self.path(operation_id) / "dependencies.tar").unlink(missing_ok=True)
                compact_result(record)
            if "logs" in selected:
                (self.path(operation_id) / "logs.bin").unlink(missing_ok=True)
                if self.account_bridge is not None:
                    self.account_bridge.close(operation_id)
                if not (self.path(operation_id) / "account-lease.json").exists():
                    (self.path(operation_id) / "account-protection.json").unlink(
                        missing_ok=True
                    )
            if "source" in selected:
                for key in ("source_digest", "context_digest"):
                    value = record["manifest"][key]
                    referenced = any(
                        value
                        in (
                            other.get("manifest", {}).get("source_digest"),
                            other.get("manifest", {}).get("context_digest"),
                        )
                        and "source" not in other.get("resources_removed", [])
                        for other in (
                            self.record(path.name) for path in self.operations.iterdir()
                        )
                    )
                    if not referenced:
                        (self.artifacts / value).unlink(missing_ok=True)
            record["tombstone"] = done == {"source", "logs"}
            self.save(record)
            return self.public(record)

    def capabilities(self):
        from .slot_safety import capabilities

        return capabilities(self)

    def profile_for(self, data):
        if (
            self.qualifying
            and self.probe_profile
            and data.get("operation_id") == self.probe_operation
        ):
            if data.get("profile_id") == self.probe_profile.profile_id:
                return self.probe_profile
        return self.profiles.get(data.get("profile_id"))

    def qualification(self, action):
        from .slot_qualification import qualify, prepare_restart, progress

        if action == "status":
            return progress(self)
        if action == "run" and progress(self)["state"] in {"completed", "failed"}:
            return progress(self)
        if not self.qualification_lock.acquire(blocking=False):
            return progress(self)
        try:
            if self.active:
                from .slot_qualification import restart_witness

                if action == "restart":
                    return restart_witness(self)
                raise Conflict("Stop active work before qualifying this slot.")
            return prepare_restart(self) if action == "restart" else qualify(self)
        finally:
            self.qualification_lock.release()

    def dependency_chunk(self, operation_id, offset, limit):
        from .retention import require_available

        require_available(self.record(operation_id), "source")
        if (
            type(offset) is not int
            or type(limit) is not int
            or offset < 0
            or not 1 <= limit <= 65536
        ):
            raise Rejected("Invalid dependency artifact byte range.")
        record = self.record(operation_id)
        if (
            record["state"] != "exited"
            or not record.get("descendants_terminated")
            or record.get("result", {}).get("exit_code") != 0
            or not record.get("result", {}).get("dependency_artifact")
        ):
            raise Conflict("No terminated dependency artifact is available.")
        with (self.path(operation_id) / "dependencies.tar").open("rb") as stream:
            stream.seek(offset)
            return stream.read(limit)
