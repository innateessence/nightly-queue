"""Resolve and transfer dependencies through fixed slots, without image builds."""

import base64
from dataclasses import replace
import json
import os
import threading

from .artifacts import unpack
from .contracts import (
    Conflict,
    NotFound,
    Rejected,
    canonical,
    digest,
    identifier,
    sha256,
)
from .dependency_artifacts import MAX_DEPENDENCIES, manifest
from .runtime import durable
from .validation_profile import PACKAGE_HOSTS, ValidationProfile


class DependencyBuilds:
    def __init__(self, gateway):
        self.gateway = gateway
        self.directory = gateway.directory / "dependency-builds"
        self.directory.mkdir(mode=0o700, exist_ok=True)
        self.lock = threading.RLock()

    def path(self, build_id):
        return self.directory / identifier(build_id)

    def read(self, build_id):
        path = self.path(build_id) / "record.json"
        if not path.exists():
            raise NotFound("This dependency build was never prepared.")
        return json.loads(path.read_bytes())

    def save(self, record):
        durable(
            self.path(record["request"]["build_id"]) / "record.json", canonical(record)
        )

    @staticmethod
    def public(record):
        return {
            key: value
            for key, value in record.items()
            if key
            in {
                "state",
                "profile",
                "provenance",
                "wait_reason",
                "resources_removed",
                "tombstone",
            }
        }

    def prepare(self, data):
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
            raise Rejected("Supply the complete fixed dependency request.")
        identifier(data["run_id"])
        build_id = identifier(data["build_id"])
        with self.lock:
            directory = self.path(build_id)
            if directory.exists():
                record = self.read(build_id)
                if canonical(data) != canonical(record["request"]):
                    raise Conflict("Dependency identity binds different inputs.")
                return self.public(record)
            parent = ValidationProfile.parse(data["validation_profile"])
            setup = parent.setup_profile()
            capabilities = self.gateway.capabilities()
            if not capabilities["qualified"]:
                raise Rejected(
                    "Qualify the worker fleet before constructing dependencies."
                )
            installed = {
                value["profile_id"]: value for value in capabilities["profiles"]
            }
            if (
                data["setup_profile_id"] != setup.profile_id
                or installed.get(setup.profile_id, {}).get("profile_digest")
                != setup.digest
                or data["limits"] != setup.data()["limits"]
                or data["network_hosts"] != list(PACKAGE_HOSTS)
            ):
                raise Rejected(
                    "Dependency construction must match the fixed setup slot."
                )
            sha256(data["source_digest"])
            files = unpack(
                (self.gateway.artifacts / data["source_digest"]).read_bytes()
            )
            if "requirements.txt" not in files or any(
                name.split("/", 1)[0] in {".vibearr-dependencies", ".vibearr-build-tmp"}
                for name in files
            ):
                raise Rejected(
                    "Dependency source has no requirements or occupies reserved paths."
                )
            retained = sum(
                self.read(path.name)["state"] != "purged"
                for path in self.directory.iterdir()
            )
            if retained >= 32:
                raise Rejected("Dependency journal is full; preserve active builds.")
            context = canonical(
                {
                    "schema_version": 1,
                    "kind": "dependency-build",
                    "requirements_digest": digest(files["requirements.txt"][1]),
                }
            )
            uploaded = self.gateway.upload("context", context, digest(context))
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
            self.gateway.prepare(job)
            directory.mkdir(mode=0o700)
            record = {
                "request": data,
                "state": "prepared",
                "runtime_binding": capabilities["binding_digest"],
                "requirements_digest": digest(files["requirements.txt"][1]),
                "setup_instance": self.gateway.clients["setup"].capabilities()[
                    "instance_id"
                ],
            }
            self.save(record)
            return self.public(record)

    def start(self, build_id):
        with self.lock:
            record = self.read(build_id)
            if record["state"] == "prepared":
                record["state"] = "running"
                self.save(record)
            if record["state"] == "running":
                self.gateway.start(build_id)
            return self.public(record)

    def status(self, build_id):
        with self.lock:
            record = self.read(build_id)
            if record["state"] != "running":
                return self.public(record)
            observed = self.gateway.status(build_id)
            if observed["state"] == "not-started":
                self.gateway.start(build_id)
                return self.public(record)
            if observed["state"] in {"not-started", "running"}:
                return self.public(record)
            result = observed.get("result", {})
            if (
                observed["state"] != "exited"
                or observed.get("descendants_terminated") is not True
                or result.get("exit_code") != 0
                or result.get("timed_out")
                or result.get("canceled")
                or result.get("export_error")
                or not result.get("dependency_artifact")
            ):
                record["state"] = (
                    "failed" if observed["state"] == "exited" else "unknown"
                )
                self.save(record)
                return self.public(record)
            self.publish(record, observed)
            return self.public(record)

    def publish(self, record, observed):
        current = self.gateway.capabilities()
        if (
            not current["qualified"]
            or current["binding_digest"] != record["runtime_binding"]
        ):
            raise Conflict(
                "Dependency construction lost its original qualified fleet binding."
            )
        build_id = record["request"]["build_id"]
        result = observed["result"]
        expected = result["dependency_artifact"]
        if (
            expected.get("requirements_digest") != record["requirements_digest"]
            or type(expected.get("bytes")) is not int
            or not 0 < expected["bytes"] <= MAX_DEPENDENCIES
        ):
            raise Rejected("Dependency output differs from the requested requirements.")
        client = self.gateway.client(build_id)
        path = self.path(build_id) / "artifact.tar"
        with path.open("wb") as stream:
            offset = 0
            while offset < expected["bytes"]:
                response = client.request(
                    f"/v2/operations/{build_id}/dependencies",
                    {"offset": offset, "limit": 65536},
                )
                content = base64.b64decode(response["content"], validate=True)
                if (
                    not 1 <= len(content) <= 65536
                    or response["next_offset"] != offset + len(content)
                    or offset + len(content) > expected["bytes"]
                ):
                    raise Rejected(
                        "Dependency download returned an invalid byte range."
                    )
                stream.write(content)
                offset += len(content)
            stream.flush()
            os.fsync(stream.fileno())
        if canonical(manifest(path.read_bytes())) != canonical(expected):
            raise Rejected(
                "Dependency download failed complete digest and inventory verification."
            )
        parent = ValidationProfile.parse(record["request"]["validation_profile"])
        profile = replace(parent, dependency_digest=expected["requirements_digest"])
        target = self.gateway.clients["validation"]
        transfer = {
            "build_id": build_id,
            "run_id": record["request"]["run_id"],
            "artifact": expected,
            "profile": profile.data(),
        }
        state = target.request("/v2/dependencies/prepare", transfer)
        with path.open("rb") as stream:
            offset = state["offset"]
            if type(offset) is not int or not 0 <= offset <= expected["bytes"]:
                raise Rejected(
                    "Dependency destination returned an invalid transfer offset."
                )
            stream.seek(offset)
            while offset < expected["bytes"]:
                content = stream.read(65536)
                state = target.request(
                    f"/v2/dependencies/{build_id}/chunks",
                    {"offset": offset, "content": base64.b64encode(content).decode()},
                )
                offset += len(content)
                if state["offset"] != offset:
                    raise Rejected(
                        "Dependency destination did not acknowledge the bound chunk."
                    )
        current = self.gateway.capabilities()
        if (
            state["state"] != "ready"
            or not current["qualified"]
            or current["binding_digest"] != record["runtime_binding"]
        ):
            raise Rejected(
                "Dependency transfer or current worker qualification is incomplete."
            )
        record["profile"] = profile.data()
        record["provenance"] = {
            "schema_version": 2,
            "build_id": build_id,
            "run_id": record["request"]["run_id"],
            "source_digest": record["request"]["source_digest"],
            "requirements_digest": record["requirements_digest"],
            "dependency_digest": expected["sha256"],
            "parent_image": parent.image,
            "parent_profile_digest": parent.digest,
            "parent_runtime_binding": record["runtime_binding"],
            "setup_operation_id": build_id,
            "setup_log_digest": result["log_digest"],
            "artifact": expected,
            "validation_profile_digest": profile.digest,
            "setup_instance_id": record["setup_instance"],
            "setup_worker_boot": observed["worker_boot"],
        }
        record["state"] = "ready"
        self.save(record)

    def stop(self, build_id):
        with self.lock:
            record = self.read(build_id)
            if record["state"] in {"ready", "failed", "canceled", "purged"}:
                return self.public(record)
            self.gateway.stop(build_id)
            observed = self.gateway.status(build_id)
            record["state"] = (
                "canceled" if observed.get("descendants_terminated") else "unknown"
            )
            self.save(record)
            return self.public(record)

    def pause(self, build_id):
        return self.gateway.pause(build_id)

    def resume(self, build_id):
        return self.gateway.resume(build_id)

    def purge(self, build_id, run_id, resources):
        with self.lock:
            record = self.read(build_id)
            if record["request"]["run_id"] != identifier(run_id) or record[
                "state"
            ] not in {"ready", "failed", "canceled", "purged"}:
                raise Conflict(
                    "Only the owning terminal run can remove dependency artifacts."
                )
            self.gateway.purge(build_id, run_id, resources)
            if "source" in resources:
                self.gateway.clients["validation"].request(
                    f"/v2/dependencies/{build_id}/purge", {"run_id": run_id}
                )
                (self.path(build_id) / "artifact.tar").unlink(missing_ok=True)
            record["resources_removed"] = sorted(
                set(resources) | set(record.get("resources_removed", []))
            )
            if set(record["resources_removed"]) == {"source", "logs"}:
                record.update(state="purged", tombstone=True)
            self.save(record)
            return self.public(record)
