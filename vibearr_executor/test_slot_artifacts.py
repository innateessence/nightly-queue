from dataclasses import replace
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

from .contracts import ArtifactRemoved, Conflict, NotFound, Rejected, digest
from .dependency_artifacts import capture, manifest
from .slot_artifacts import DependencyArtifacts
from .validation_profile import ValidationProfile


class DependencyTransferTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        environment = root / "environment"
        (environment / "bin").mkdir(parents=True)
        (environment / "bin/python").write_bytes(b"fixture interpreter, never executed")
        (environment / "pyvenv.cfg").write_text(
            "include-system-site-packages = false\n"
        )
        (environment / "resolved.txt").write_text("six==1.17.0\n")
        self.content = capture(environment, b"six==1.17.0\n")
        self.profile = ValidationProfile(
            "sha256:" + "a" * 64, "sha256:" + "b" * 64, digest(b"six==1.17.0\n")
        )
        runtime = SimpleNamespace(
            directory=root,
            lock=threading.RLock(),
            profiles={
                profile.profile_id: profile
                for profile in self.profile.executor_profiles()
            },
            active="",
        )
        self.store = DependencyArtifacts(runtime)
        self.request = {
            "build_id": str(uuid4()),
            "run_id": str(uuid4()),
            "artifact": manifest(self.content),
            "profile": self.profile.data(),
        }

    def test_changed_chunk_cannot_rebind_a_partial_transfer(self):
        self.store.prepare(self.request)
        self.store.append(self.request["build_id"], 0, self.content[:512])
        with self.assertRaises(Conflict):
            self.store.append(self.request["build_id"], 0, b"changed")

    def test_complete_transfer_checks_the_inventory_and_digest(self):
        self.store.prepare(self.request)
        result = self.store.append(self.request["build_id"], 0, self.content)
        self.assertEqual(result["state"], "ready")

    def test_altered_complete_payload_is_never_ready(self):
        self.store.prepare(self.request)
        changed = b"x" + self.content[1:]
        with self.assertRaises(Rejected):
            self.store.append(self.request["build_id"], 0, changed)
        self.assertEqual(
            self.store.read(self.request["build_id"])["state"], "receiving"
        )

    def test_artifact_cannot_change_its_owning_run(self):
        self.store.prepare(self.request)
        self.request["run_id"] = str(uuid4())
        with self.assertRaises(Conflict):
            self.store.prepare(self.request)

    def test_other_image_cannot_be_selected_by_artifact_metadata(self):
        self.request["profile"] = replace(
            self.profile, image="sha256:" + "c" * 64
        ).data()
        with self.assertRaises(Rejected):
            self.store.prepare(self.request)

    def test_purged_artifact_cannot_be_reconstructed_by_repeating_prepare(self):
        self.store.prepare(self.request)
        self.store.purge(self.request["build_id"], self.request["run_id"])
        with self.assertRaises(ArtifactRemoved):
            self.store.prepare(self.request)

    def test_active_validation_protects_dependencies_from_cleanup(self):
        self.store.prepare(self.request)
        self.store.runtime.active = str(uuid4())
        with self.assertRaises(Conflict):
            self.store.purge(self.request["build_id"], self.request["run_id"])

    def test_unfinished_transfers_reserve_their_complete_size(self):
        self.store.prepare(self.request)
        another = {**self.request, "build_id": str(uuid4())}
        with patch("vibearr_executor.slot_artifacts.MAX_STORAGE", len(self.content)):
            with self.assertRaises(Rejected):
                self.store.prepare(another)

    def test_repeated_prepare_still_succeeds_at_capacity(self):
        self.store.prepare(self.request)
        with patch("vibearr_executor.slot_artifacts.MAX_STORAGE", 0):
            self.assertEqual(self.store.prepare(self.request)["offset"], 0)

    def test_unknown_transfer_has_an_explicit_not_found_result(self):
        with self.assertRaises(NotFound):
            self.store.append(str(uuid4()), 0, b"chunk")
