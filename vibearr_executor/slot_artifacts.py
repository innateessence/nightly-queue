"""Run-scoped dependency artifacts cannot replace image-owned tools."""

import json
import os
from pathlib import Path

from .contracts import (
    ArtifactRemoved,
    Conflict,
    NotFound,
    Rejected,
    canonical,
    digest,
    identifier,
    sha256,
)
from .dependency_artifacts import (
    MAX_DEPENDENCIES,
    MAX_DEPENDENCY_FILES,
    manifest,
    read_layer,
)
from .runtime import durable
from .validation_profile import ValidationProfile


MAX_STORAGE = 2 * 1024**3


class DependencyArtifacts:
    def __init__(self, runtime):
        self.runtime = runtime
        self.directory = runtime.directory / "dependency-artifacts"
        self.directory.mkdir(mode=0o700, exist_ok=True)

    def path(self, build_id):
        return self.directory / identifier(build_id)

    def read(self, build_id):
        path = self.path(build_id) / "record.json"
        if not path.exists():
            raise NotFound("This dependency transfer was never prepared.")
        return json.loads(path.read_bytes())

    def prepare(self, data):
        if set(data) != {"build_id", "run_id", "artifact", "profile"}:
            raise Rejected("Supply the complete run-bound dependency artifact.")
        identifier(data["run_id"])
        directory = self.path(data["build_id"])
        profile = ValidationProfile.parse(data["profile"])
        artifact = data["artifact"]
        if set(artifact) != {
            "schema_version",
            "sha256",
            "bytes",
            "files",
            "requirements_digest",
        }:
            raise Rejected("Dependency artifact metadata is incomplete.")
        sha256(artifact["sha256"])
        sha256(artifact["requirements_digest"])
        if (
            type(artifact["schema_version"]) is not int
            or artifact["schema_version"] != 1
            or type(artifact["bytes"]) is not int
            or not 1 <= artifact["bytes"] <= MAX_DEPENDENCIES
            or type(artifact["files"]) is not int
            or not 1 <= artifact["files"] <= MAX_DEPENDENCY_FILES
        ):
            raise Rejected("Dependency artifact exceeds its fixed bounds.")
        if artifact["requirements_digest"] != profile.dependency_digest:
            raise Rejected("Dependency requirements differ from the selected profile.")
        if any(
            self.runtime.profiles.get(value.profile_id) != value
            for value in profile.executor_profiles()
        ):
            raise Rejected(
                "Only the installed validation image may consume an artifact."
            )
        with self.runtime.lock:
            if directory.exists():
                record = self.read(data["build_id"])
                if record["state"] == "purged":
                    raise ArtifactRemoved(
                        "Dependency artifact was removed by retention."
                    )
                if canonical(record["request"]) != canonical(data):
                    raise Conflict(
                        "Artifact identity already binds a different run or payload."
                    )
                return {
                    "state": record["state"],
                    "offset": (directory / "payload.tar").stat().st_size,
                }
            records = (self.read(path.name) for path in self.directory.iterdir())
            retained = [value for value in records if value["state"] != "purged"]
            reserved = sum(value["request"]["artifact"]["bytes"] for value in retained)
            if reserved + artifact["bytes"] > MAX_STORAGE or len(retained) >= 64:
                raise Rejected(
                    "Dependency artifact storage is full; retain active data."
                )
            directory.mkdir(mode=0o700)
            durable(directory / "payload.tar", b"")
            durable(
                directory / "record.json",
                canonical({"request": data, "state": "receiving"}),
            )
            return {"state": "receiving", "offset": 0}

    def append(self, build_id, offset, content):
        if type(offset) is not int or offset < 0 or not 1 <= len(content) <= 65536:
            raise Rejected("Invalid bounded dependency chunk.")
        with self.runtime.lock:
            record = self.read(build_id)
            if record["state"] == "purged":
                raise ArtifactRemoved("Dependency artifact was removed by retention.")
            expected = record["request"]["artifact"]
            path = self.path(build_id) / "payload.tar"
            size = path.stat().st_size
            if offset > size or offset + len(content) > expected["bytes"]:
                raise Conflict(
                    "Dependency chunk offset differs from the bound transfer."
                )
            if offset < size:
                with path.open("rb") as stream:
                    stream.seek(offset)
                    if stream.read(len(content)) != content:
                        raise Conflict("Repeated dependency chunk differs.")
            elif record["state"] == "receiving":
                with path.open("ab") as stream:
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
            if path.stat().st_size == expected["bytes"]:
                if canonical(manifest(path.read_bytes())) != canonical(expected):
                    raise Rejected(
                        "Transferred dependencies failed digest and inventory verification."
                    )
                record["state"] = "ready"
                durable(self.path(build_id) / "record.json", canonical(record))
            return {"state": record["state"], "offset": path.stat().st_size}

    def install(self, run_id, requirements_digest):
        matches = []
        for path in self.directory.iterdir():
            record = self.read(path.name)
            request = record["request"]
            if (
                record["state"] == "ready"
                and request["run_id"] == run_id
                and request["artifact"]["requirements_digest"] == requirements_digest
            ):
                matches.append((path, request))
        if not matches:
            raise Rejected(
                "Build and transfer the exact run-scoped candidate dependencies first."
            )
        if len({request["artifact"]["sha256"] for _, request in matches}) != 1:
            raise Conflict(
                "Multiple dependency payloads claim the same run and requirements."
            )
        path, request = matches[0]
        content = (path / "payload.tar").read_bytes()
        if digest(content) != request["artifact"]["sha256"]:
            raise Rejected("Saved dependency artifact changed.")
        for name, (mode, value) in read_layer(content).items():
            target = Path("/opt/candidate") / name
            target.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(
                target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o444
            )
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(value)
            target.chmod(0o555 if mode & 0o111 else 0o444)
        for path in Path("/opt/candidate").rglob("*"):
            if path.is_dir():
                path.chmod(0o755)
        Path("/opt/candidate").chmod(0o755)
        return request["artifact"]

    def purge(self, build_id, run_id):
        with self.runtime.lock:
            if not (self.path(build_id) / "record.json").exists():
                return {"state": "not-stored"}
            record = self.read(build_id)
            if record["request"]["run_id"] != identifier(run_id):
                raise Conflict("Dependency retention run differs.")
            if self.runtime.active:
                raise Conflict(
                    "Dependency artifacts remain protected while a validation job runs."
                )
            record["state"] = "purged"
            durable(self.path(build_id) / "record.json", canonical(record))
            (self.path(build_id) / "payload.tar").unlink(missing_ok=True)
            return {"state": "purged"}
