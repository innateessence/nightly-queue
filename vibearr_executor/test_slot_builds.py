from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import threading
import unittest
from uuid import uuid4

from .artifacts import pack
from .contracts import Conflict, digest
from .dependency_gateway import DependencyGateway
from .slot_builds import DependencyBuilds
from .validation_profile import PACKAGE_HOSTS, ValidationProfile


class Worker:
    def __init__(self, profiles, directory):
        self.profiles = profiles
        self.directory = directory
        self.artifacts = directory / "artifacts"
        self.artifacts.mkdir()
        self.lock = threading.RLock()
        self.instance = str(uuid4())
        self.binding = "a" * 64
        self.operations = {}
        self.started = []

    def capabilities(self):
        return {
            "runtime": "compose-slot-v2",
            "protocol_version": 2,
            "instance_id": self.instance,
            "binding_digest": self.binding,
            "qualified": True,
            "accepting_work": True,
            "profiles": [
                {**profile.data(), "profile_digest": profile.digest}
                for profile in self.profiles
            ],
        }

    def upload(self, _kind, content, expected):
        assert digest(content) == expected
        (self.artifacts / expected).write_bytes(content)
        return {"sha256": expected}

    def prepare(self, data):
        return self.operations.setdefault(
            data["operation_id"], {"state": "not-started"}
        )

    def start(self, operation):
        self.started.append(operation)
        self.operations[operation] = {"state": "running"}
        return self.status(operation)

    def status(self, operation):
        return self.operations[operation]

    def stop(self, operation):
        self.operations[operation] = {"state": "exited", "descendants_terminated": True}


class DependencyBuildLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.parent = ValidationProfile(
            "sha256:" + "b" * 64, "sha256:" + "c" * 64, digest(b"base")
        )
        self.setup = Worker(
            [self.parent.setup_profile(), *self.parent.executor_profiles()],
            Path(self.temporary.name),
        )
        self.validation = self.setup
        self.gateway = DependencyGateway(self.setup)
        self.setup.dependency_builds = DependencyBuilds(self.gateway)
        source = pack({"requirements.txt": (0o644, b"six==1.17.0\n")})
        self.gateway.upload("source", source, digest(source))
        self.request = {
            "schema_version": 1,
            "run_id": str(uuid4()),
            "build_id": str(uuid4()),
            "source_digest": digest(source),
            "setup_profile_id": self.parent.setup_profile().profile_id,
            "validation_profile": self.parent.data(),
            "limits": self.parent.setup_profile().data()["limits"],
            "deadline": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
            "network_hosts": list(PACKAGE_HOSTS),
        }
        self.builds = self.gateway.dependency_builds
        self.identity = self.request["build_id"]
        self.builds.prepare(self.request)

    def test_lost_dispatch_response_retries_only_the_same_prepared_operation(self):
        record = self.builds.read(self.identity)
        record["state"] = "running"
        self.builds.save(record)
        self.assertEqual(self.builds.status(self.identity)["state"], "running")
        self.assertEqual(self.setup.started, [self.identity])

    def test_unknown_setup_is_preserved_without_starting_a_replacement(self):
        self.builds.start(self.identity)
        self.setup.operations[self.identity] = {"state": "unknown"}
        self.assertEqual(self.builds.status(self.identity)["state"], "unknown")
        self.assertEqual(self.setup.started, [self.identity])

    def test_nonzero_setup_exit_cannot_publish_a_dependency_profile(self):
        self.builds.start(self.identity)
        self.setup.operations[self.identity] = {
            "state": "exited",
            "descendants_terminated": True,
            "result": {"exit_code": 1},
        }
        observed = self.builds.status(self.identity)
        self.assertEqual(observed["state"], "failed")
        self.assertNotIn("profile", observed)

    def test_ready_build_is_not_changed_by_a_repeated_stop(self):
        record = self.builds.read(self.identity)
        record["state"] = "ready"
        self.builds.save(record)
        self.assertEqual(self.builds.stop(self.identity)["state"], "ready")
        self.assertEqual(self.setup.started, [])

    def test_changed_worker_boot_cannot_publish_under_the_previous_binding(self):
        self.setup.binding = "d" * 64
        with self.assertRaises(Conflict):
            self.builds.publish(self.builds.read(self.identity), {})
