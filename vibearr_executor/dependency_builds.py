"""Journaled setup, copy-only OCI assembly, and independent image qualification."""

from datetime import datetime
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

from .artifacts import unpack
from .contracts import Conflict, Rejected, canonical, digest, identifier, sha256
from .dependency_artifacts import MAX_DEPENDENCIES, manifest, read_layer
from .engine import EngineError
from .validation_profile import BUILD_ARGV, ValidationProfile


class DependencyBuilds:
    def __init__(self, runtime):
        self.runtime = runtime
        self.directory = runtime.directory / "dependency-builds"
        self.directory.mkdir(mode=0o700, exist_ok=True)
        self.threads: dict[str, threading.Thread] = {}

    def path(self, build_id):
        return self.directory / identifier(build_id)

    def read(self, build_id):
        path = self.path(build_id) / "record.json"
        if not path.exists():
            raise Rejected("Prepare this dependency build first.")
        return json.loads(path.read_bytes())

    def save(self, record):
        from .runtime import durable

        durable(
            self.path(record["request"]["build_id"]) / "record.json", canonical(record)
        )

    def blocked(self, operation_id):
        return any(
            self.read(path.name)["state"] not in {"ready", "failed", "canceled"}
            and path.name != operation_id
            for path in self.directory.iterdir()
        )

    def prepare(self, data):
        from .qualification import binding, capabilities
        from .runtime import durable

        expected = {
            "schema_version",
            "run_id",
            "build_id",
            "source_digest",
            "setup_profile_id",
            "validation_profile",
            "limits",
            "deadline",
            "network_hosts",
        }
        if (
            not isinstance(data, dict)
            or set(data) != expected
            or type(data["schema_version"]) is not int
            or data["schema_version"] != 1
        ):
            raise Rejected("Supply the complete fixed dependency-build request.")
        identifier(data["run_id"])
        build_id = identifier(data["build_id"])
        path = self.path(build_id)
        with self.runtime.lock:
            if path.exists():
                saved = self.read(build_id)
                if canonical(saved["request"]) != canonical(data):
                    raise Conflict("Build identity already binds different inputs.")
                return self.public(saved)
            profile = ValidationProfile.parse(data["validation_profile"])
            setup = self.runtime.profiles.get(data["setup_profile_id"])
            if setup is None or setup.data() != profile.setup_profile().data():
                raise Rejected(
                    "Select the exact registered credential-free build recipe."
                )
            if any(
                self.runtime.profiles.get(value.profile_id) != value
                for value in profile.executor_profiles()
            ):
                raise Rejected("The parent validation profiles must be registered.")
            if (
                tuple(setup.argv) != BUILD_ARGV
                or not capabilities(self.runtime)["qualified"]
            ):
                raise Rejected(
                    "Qualify the registered parent image before building dependencies."
                )
            sha256(data["source_digest"])
            source_path = self.runtime.artifacts / data["source_digest"]
            source = source_path.read_bytes() if source_path.is_file() else b""
            if digest(source) != data["source_digest"]:
                raise Rejected("Upload the exact committed candidate source first.")
            files = unpack(source)
            requirements = files.get("requirements.txt")
            if requirements is None:
                raise Rejected("The candidate requirements file is missing.")
            if any(
                name.split("/", 1)[0] in {".vibearr-dependencies", ".vibearr-build-tmp"}
                for name in files
            ):
                raise Rejected(
                    "Candidate source occupies the reserved dependency workspace."
                )
            if len(list(self.directory.iterdir())) >= 32:
                raise Rejected(
                    "Dependency-build journal is full; retain uncertain work and archive completed builds."
                )
            usage = sum(
                file.stat().st_size
                for file in self.directory.rglob("*")
                if file.is_file()
            )
            storage = os.statvfs(self.directory)
            if (
                usage + MAX_DEPENDENCIES > 2 * 1024**3
                or storage.f_bavail * storage.f_frsize
                < MAX_DEPENDENCIES + 128 * 1024**2
            ):
                raise Rejected(
                    "Dependency image construction has insufficient journal capacity."
                )
            context = canonical(
                {
                    "schema_version": 1,
                    "kind": "dependency-build",
                    "requirements_digest": digest(requirements[1]),
                }
            )
            uploaded = self.runtime.upload("context", context, digest(context))
            job = {
                "schema_version": 1,
                "run_id": data["run_id"],
                "operation_id": build_id,
                "profile_id": setup.profile_id,
                "profile_digest": setup.digest,
                "source_digest": data["source_digest"],
                "context_digest": uploaded["sha256"],
                "phase": "setup",
                "limits": data["limits"],
                "deadline": data["deadline"],
                "network_hosts": data["network_hosts"],
            }
            self.runtime.prepare(job)
            path.mkdir(mode=0o700)
            durable(path / "requirements.txt", requirements[1])
            saved = {
                "request": data,
                "state": "prepared",
                "parent_binding": digest(canonical(binding(self.runtime))),
                "requirements_digest": digest(requirements[1]),
                "setup_manifest": job,
                "assembly_name": f"vibearr-deps-{self.runtime.instance[:8]}-{build_id}",
                "assembly_intent": False,
            }
            self.save(saved)
            return self.public(saved)

    def start(self, build_id):
        with self.runtime.lock:
            record = self.read(build_id)
            if record["state"] in {"ready", "failed", "canceled", "unknown"}:
                return self.public(record)
            thread = self.threads.get(build_id)
            if thread is None or not thread.is_alive():
                thread = threading.Thread(
                    target=self.run, args=(build_id,), daemon=True
                )
                self.threads[build_id] = thread
                thread.start()
            return self.public(record)

    @staticmethod
    def public(record):
        keys = (
            "state",
            "image",
            "profile",
            "requirements_digest",
            "dependency_digest",
            "wait_reason",
        )
        value = {key: record[key] for key in keys if key in record}
        value["build_id"] = record["request"]["build_id"]
        value["run_id"] = record["request"]["run_id"]
        if record["state"] == "ready" and record.get("provenance"):
            value["provenance"] = record["provenance"]
        return value

    def status(self, build_id):
        with self.runtime.lock:
            record = self.read(build_id)
            if (
                record["state"] == "unknown"
                and record.get("image_commit_intent")
                and not record.get("cancel_requested")
            ):
                try:
                    image = self.committed_image(record)
                    if image is not None:
                        self.adopt_image(record, image)
                        self.start(build_id)
                        record = self.read(build_id)
                except EngineError:
                    pass
            return self.public(record)

    def processes(self, state):
        result = []
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            try:
                if entry.stat().st_uid != os.getuid():
                    continue
                arguments = (entry / "cmdline").read_bytes().split(b"\0")
            except (FileNotFoundError, ProcessLookupError, PermissionError):
                continue
            expected = [
                b"-m",
                b"vibearr_executor.dependency_qualify",
                b"--state",
                str(state).encode(),
            ]
            if any(
                arguments[index : index + 4] == expected
                for index in range(len(arguments) - 3)
            ):
                result.append(int(entry.name))
        return result

    def run(self, build_id):
        from .qualification import binding
        from .runtime import durable

        try:
            while True:
                with self.runtime.lock:
                    record = self.read(build_id)
                    if record.get("cancel_requested") or record["state"] in {
                        "ready",
                        "failed",
                        "canceled",
                        "unknown",
                    }:
                        return
                    if (
                        digest(canonical(binding(self.runtime)))
                        != record["parent_binding"]
                    ):
                        raise Rejected(
                            "Parent runtime/profile binding changed during dependency construction."
                        )
                    if (
                        time.time()
                        >= datetime.fromisoformat(
                            record["request"]["deadline"]
                        ).timestamp()
                    ):
                        raise TimeoutError("Dependency-build deadline expired.")
                    state = record["state"]
                    if state in {"prepared", "setup"}:
                        observed = self.runtime.start(build_id)
                        if observed["state"] == "unknown":
                            raise EngineError(
                                "Setup outcome is uncertain; reconcile the same build."
                            )
                        if observed["state"] != "exited":
                            record["state"] = "setup"
                            self.save(record)
                            time.sleep(0.5)
                            continue
                        result = observed.get("result", {})
                        if (
                            result.get("exit_code") != 0
                            or result.get("timed_out")
                            or result.get("canceled")
                            or not observed.get("descendants_terminated")
                            or not result.get("dependency_artifact")
                        ):
                            raise Rejected(
                                "Isolated dependency setup failed; inspect its protected logs."
                            )
                        content = (
                            self.runtime.path(build_id) / "dependencies.tar"
                        ).read_bytes()
                        expected = manifest(content)
                        if (
                            expected != result["dependency_artifact"]
                            or expected["requirements_digest"]
                            != record["requirements_digest"]
                        ):
                            raise Rejected(
                                "Dependency layer differs from the protected setup result."
                            )
                        record.update(
                            state="assembling", dependency_digest=expected["sha256"]
                        )
                        self.save(record)
                        continue
                    if state == "assembling":
                        self.assemble(record)
                        continue
                    if state == "qualifying":
                        proof_state = self.path(build_id) / "qualification"
                        complete = self.path(build_id) / "qualification-exit.json"
                        processes = self.processes(proof_state)
                        if complete.exists() and not processes:
                            if not json.loads(complete.read_bytes()).get(
                                "success"
                            ) or not self.qualified(record):
                                raise Rejected(
                                    "Derived image failed actual host boundary qualification."
                                )
                            record["provenance"] = self.provenance(record)
                            record["state"] = "ready"
                            self.save(record)
                            return
                        if not processes:
                            if record.get("qualification_intent"):
                                raise EngineError(
                                    "Derived-image qualification outcome is unknown; stop/reconcile this build."
                                )
                            profile = self.path(build_id) / "profile.json"
                            durable(profile, canonical(record["profile"]))
                            command = [
                                sys.executable,
                                "-I",
                                "-m",
                                "vibearr_executor.dependency_qualify",
                                "--state",
                                str(proof_state),
                                "--profile",
                                str(profile),
                                "--deadline",
                                str(
                                    datetime.fromisoformat(
                                        record["request"]["deadline"]
                                    ).timestamp()
                                ),
                            ]
                            for address in self.runtime.configured_ips:
                                command.extend(("--deny-ip", address))
                            record["qualification_intent"] = True
                            self.save(record)
                            with (self.path(build_id) / "qualification.log").open(
                                "ab"
                            ) as log:
                                subprocess.Popen(
                                    command,
                                    stdout=log,
                                    stderr=log,
                                    start_new_session=True,
                                )
                        time.sleep(0.5)
        except Exception as exc:
            with self.runtime.lock:
                record = self.read(build_id)
                if record.get("cancel_requested") or record["state"] == "canceled":
                    return
                operation = self.runtime.record(build_id)
                active = (
                    operation.get("launch_intent")
                    and operation.get("state") != "exited"
                )
                uncertain = active or isinstance(
                    exc, (EngineError, OSError, TimeoutError)
                )
                record.update(
                    state="unknown" if uncertain else "failed",
                    wait_reason=str(exc)[:400],
                )
                self.save(record)

    def assembly_labels(self, record):
        return {
            "vibearr.executor": self.runtime.instance,
            "vibearr.dependency-build": record["request"]["build_id"],
            "vibearr.dependency-layer": record["dependency_digest"],
            "vibearr.parent-image": record["request"]["validation_profile"]["image"],
        }

    def committed_image(self, record):
        labels = self.assembly_labels(record)
        parent_id = record["request"]["validation_profile"]["image"]
        parent = json.loads(self.runtime.docker.run("image", "inspect", parent_id))[0]
        images = (
            self.runtime.docker.run(
                "image",
                "ls",
                "--no-trunc",
                "--quiet",
                "--filter",
                "label=vibearr.dependency-build=" + record["request"]["build_id"],
            )
            .decode()
            .splitlines()
        )
        matches = set()
        for image in images:
            current = json.loads(self.runtime.docker.run("image", "inspect", image))[0]
            configured = current["Config"].get("Labels", {})
            if any(configured.get(key) != value for key, value in labels.items()):
                continue
            if current.get("Parent") != parent_id:
                raise EngineError("Committed dependency image has a different parent.")
            original_layers = parent["RootFS"]["Layers"]
            derived_layers = current["RootFS"]["Layers"]
            if derived_layers[:-1] != original_layers:
                raise EngineError(
                    "Committed dependency image has an unexpected layer lineage."
                )
            keys = (
                "Entrypoint",
                "Cmd",
                "Env",
                "User",
                "WorkingDir",
                "Volumes",
                "ExposedPorts",
            )
            if any(
                current["Config"].get(key) != parent["Config"].get(key) for key in keys
            ):
                raise EngineError(
                    "Committed dependency image changed protected runtime configuration."
                )
            matches.add(current["Id"])
        if len(matches) > 1:
            raise EngineError(
                "Multiple committed images match this build; preserve the fence and reconcile."
            )
        return next(iter(matches), None)

    def adopt_image(self, record, image):
        profile = ValidationProfile(
            image,
            record["request"]["validation_profile"]["database_image"],
            record["requirements_digest"],
        )
        record.update(image=image, profile=profile.data(), state="qualifying")
        self.save(record)

    def assemble(self, record):
        content = (
            self.runtime.path(record["request"]["build_id"]) / "dependencies.tar"
        ).read_bytes()
        files = read_layer(content)
        if (
            digest(content) != record["dependency_digest"]
            or digest(files["requirements.txt"][1]) != record["requirements_digest"]
        ):
            raise Rejected("Dependency layer changed before copy-only assembly.")
        name = record["assembly_name"]
        labels = self.assembly_labels(record)
        record["assembly_intent"] = True
        self.save(record)
        image = self.committed_image(record)
        if image is not None:
            self.adopt_image(record, image)
            return
        if record.get("image_commit_intent"):
            raise EngineError(
                "Image commit outcome is uncertain; wait for the original commit to reconcile."
            )
        current = self.runtime.docker.inspect(name)
        if current is not None:
            changed = (
                record.get("assembly_container_id")
                and current["Id"] != record["assembly_container_id"]
            )
            if (
                changed
                or current["State"]["Status"] != "created"
                or current["Image"] != labels["vibearr.parent-image"]
                or any(
                    current["Config"].get("Labels", {}).get(key) != value
                    for key, value in labels.items()
                )
            ):
                raise EngineError(
                    "Copy-only assembly container identity or parent changed."
                )
        if current is None:
            if record.get("assembly_container_id"):
                raise EngineError("The original assembly container disappeared.")
            arguments = [
                "create",
                "--name",
                name,
                "--network",
                "none",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges",
            ]
            for key, value in labels.items():
                arguments.extend(("--label", f"{key}={value}"))
            arguments.append(labels["vibearr.parent-image"])
            self.runtime.docker.run(*arguments)
            current = self.runtime.docker.inspect(name)
        if current is None or current["Image"] != labels["vibearr.parent-image"]:
            raise EngineError("Assembly creation outcome is unknown.")
        record["assembly_container_id"] = current["Id"]
        self.save(record)
        layer = self.path(record["request"]["build_id"]) / "layer"
        layer.mkdir(mode=0o700, exist_ok=True)
        for relative, (mode, value) in files.items():
            target = layer / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                if (
                    target.is_symlink()
                    or target.read_bytes() != value
                    or (0o755 if target.stat().st_mode & 0o111 else 0o644) != mode
                ):
                    raise EngineError("Private assembly layer changed during recovery.")
                continue
            descriptor = os.open(
                target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode
            )
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(value)
                stream.flush()
                os.fsync(stream.fileno())
            target.chmod(mode)
        self.runtime.docker.run(
            "cp", str(layer) + "/.", name + ":/opt/candidate/", timeout=120
        )
        current = self.runtime.docker.inspect(name)
        if (
            current is None
            or current["Id"] != record["assembly_container_id"]
            or current["Image"] != labels["vibearr.parent-image"]
            or current["State"]["Status"] != "created"
        ):
            raise EngineError("Assembly identity changed before image commit.")
        record["image_commit_intent"] = True
        self.save(record)
        reported = self.runtime.docker.run("commit", name, timeout=120).decode().strip()
        sha256(reported.removeprefix("sha256:"))
        image = self.committed_image(record)
        if image is None or image != reported:
            raise EngineError(
                "Image commit response does not match its unique verified image."
            )
        self.adopt_image(record, image)
        self.runtime.docker.remove("container", name)

    def qualified(self, record):
        from .qualification import REQUIRED_CHECKS, binding
        from .qualify import smoke_profiles

        try:
            selected = ValidationProfile.parse(record["profile"])
            profiles = (
                smoke_profiles(selected.image, selected.database_image)
                + selected.executor_profiles()
            )
            view = SimpleNamespace(
                docker=self.runtime.docker,
                profiles={profile.profile_id: profile for profile in profiles},
                current_denied_ips=self.runtime.current_denied_ips,
                max_active_runs=self.runtime.max_active_runs,
            )
            proof = json.loads(
                (
                    self.path(record["request"]["build_id"])
                    / "qualification"
                    / "qualification.json"
                ).read_bytes()
            )
            current = binding(view)
            return (
                digest(canonical(binding(self.runtime))) == record["parent_binding"]
                and canonical(proof.get("binding")) == canonical(current)
                and set(proof.get("checks", [])) == REQUIRED_CHECKS
                and set(proof.get("image_checks", {}).get(selected.image, []))
                == REQUIRED_CHECKS
                and all(
                    value["installed_contract"] for value in current["images"].values()
                )
            )
        except (KeyError, OSError, ValueError, EngineError):
            return False

    def profiles(self, *, run_id=None):
        profiles = {}
        for path in self.directory.iterdir():
            record = self.read(path.name)
            if run_id is not None and record["request"]["run_id"] != run_id:
                continue
            if record["state"] == "ready" and self.qualified(record):
                selected = ValidationProfile.parse(record["profile"])
                for profile in selected.executor_profiles():
                    old = profiles.get(profile.profile_id) or self.runtime.profiles.get(
                        profile.profile_id
                    )
                    if old is not None and old != profile:
                        raise Conflict("Derived validation profile identity collides.")
                    profiles[profile.profile_id] = profile
        return profiles

    def provenance(self, record):
        proof_path = (
            self.path(record["request"]["build_id"])
            / "qualification"
            / "qualification.json"
        )
        proof = json.loads(proof_path.read_bytes())
        operation = self.runtime.record(record["request"]["build_id"])
        parent = ValidationProfile.parse(record["request"]["validation_profile"])
        return {
            "schema_version": 1,
            "build_id": record["request"]["build_id"],
            "run_id": record["request"]["run_id"],
            "source_digest": record["request"]["source_digest"],
            "requirements_digest": record["requirements_digest"],
            "dependency_digest": record["dependency_digest"],
            "parent_image": parent.image,
            "parent_profile_digest": parent.digest,
            "parent_runtime_binding": record["parent_binding"],
            "setup_operation_id": record["request"]["build_id"],
            "setup_log_digest": operation["result"]["log_digest"],
            "derived_image": record["image"],
            "qualification_binding_digest": digest(canonical(proof["binding"])),
            "qualification_report_digest": digest(proof_path.read_bytes()),
            "boundary_checks": proof["checks"],
        }

    def stop(self, build_id):
        from .runtime import Runtime

        with self.runtime.lock:
            record = self.read(build_id)
            if record["state"] in {"ready", "canceled"}:
                return self.public(record)
            record["cancel_requested"] = True
            self.save(record)
        self.runtime.stop(build_id)
        proof_state = self.path(build_id) / "qualification"
        for pid in self.processes(proof_state):
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        deadline = time.monotonic() + 90
        while self.processes(proof_state) and time.monotonic() < deadline:
            time.sleep(0.2)
        if self.processes(proof_state):
            raise EngineError(
                "Qualification still owns resources; retain the build fence."
            )
        if proof_state.exists():
            for state in [proof_state, *proof_state.glob("image-probe-*")]:
                configuration = state / "profiles.json"
                if not configuration.exists():
                    continue
                from .contracts import Profile

                profiles = [
                    Profile.parse(value)
                    for value in json.loads(configuration.read_bytes())
                ]
                recovery = Runtime(
                    state, profiles, denied_ips=self.runtime.configured_ips
                )
                try:
                    for operation in recovery.operations.iterdir():
                        recovery.stop(operation.name)
                finally:
                    recovery.close()
        current = self.runtime.docker.inspect(record["assembly_name"])
        if current is not None:
            labels = current["Config"].get("Labels", {})
            if (
                labels.get("vibearr.executor") != self.runtime.instance
                or labels.get("vibearr.dependency-build") != build_id
            ):
                raise EngineError("Assembly ownership changed; retain the build fence.")
            self.runtime.docker.remove("container", record["assembly_name"])
        record["state"] = "canceled"
        self.save(record)
        return self.public(record)
