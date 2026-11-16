from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from .artifacts import pack
from . import build_controls
from .contracts import Conflict, Profile, Rejected, digest
from .runtime import Runtime
from .validation_profile import ValidationProfile


class BuildControlTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.profile = ValidationProfile(
            "sha256:" + "a" * 64, "sha256:" + "b" * 64, digest(b"base")
        )
        profiles = self.profile.executor_profiles() + [self.profile.setup_profile()]
        self.runtime = Runtime(Path(self.temporary.name), profiles)
        self.addCleanup(lambda: self.runtime.close())
        self.builds = self.runtime.dependency_builds
        self.source = pack({"requirements.txt": (0o644, b"idna==3.10\n")})
        self.runtime.upload("source", self.source, digest(self.source))
        self.request = {
            "schema_version": 1,
            "run_id": str(uuid4()),
            "build_id": str(uuid4()),
            "source_digest": digest(self.source),
            "setup_profile_id": self.profile.setup_profile().profile_id,
            "validation_profile": self.profile.data(),
            "limits": asdict(self.profile.setup_profile().limits),
            "deadline": (
                datetime.now(timezone.utc) + timedelta(minutes=10)
            ).isoformat(),
            "network_hosts": ["pypi.org", "files.pythonhosted.org"],
        }
        self.addCleanup(patch.stopall)
        patch(
            "vibearr_executor.qualification.capabilities",
            return_value={"qualified": True},
        ).start()
        patch(
            "vibearr_executor.qualification.binding", return_value={"host": "test"}
        ).start()
        self.builds.prepare(self.request)
        self.build_id = self.request["build_id"]

    def set_state(self, state, **extra):
        record = self.builds.read(self.build_id)
        record.update(state=state, **extra)
        self.builds.save(record)
        return record

    def cancel(self):
        return self.builds.stop(self.build_id)

    def test_pause_before_setup_never_starts_a_container(self):
        with patch.object(self.runtime.docker, "run") as engine:
            first = self.builds.pause(self.build_id)
            second = self.builds.start(self.build_id)
        self.assertTrue(first["paused"])
        self.assertTrue(second["pause_requested"])
        self.assertFalse(self.runtime.record(self.build_id)["launch_intent"])
        engine.assert_not_called()

    def test_pause_before_qualification_does_not_spawn_or_commit_again(self):
        self.set_state("qualifying", image_commit_intent=True)
        with (
            patch("vibearr_executor.dependency_builds.subprocess.Popen") as spawn,
            patch.object(self.builds, "assemble") as assemble,
        ):
            result = self.builds.pause(self.build_id)
            self.builds.start(self.build_id)
        self.assertTrue(result["paused"])
        spawn.assert_not_called()
        assemble.assert_not_called()

    def test_qualification_pause_reports_settling_without_killing_it(self):
        self.set_state("qualifying", qualification_intent=True)
        with (
            patch.object(self.builds, "processes", return_value=[123]),
            patch("vibearr_executor.dependency_builds.os.kill") as kill,
            patch("vibearr_executor.dependency_builds.subprocess.Popen") as spawn,
        ):
            result = self.builds.pause(self.build_id)
        self.assertEqual(result["pause_state"], "pausing-at-checkpoint")
        self.assertFalse(result["paused"])
        kill.assert_not_called()
        spawn.assert_not_called()

    def test_pause_intent_persists_while_assembly_holds_runtime_lock(self):
        entered, release = threading.Event(), threading.Event()

        def assembly():
            with self.runtime.lock:
                entered.set()
                release.wait(5)

        thread = threading.Thread(target=assembly)
        thread.start()
        self.assertTrue(entered.wait(2))
        self.set_state("assembling", assembly_intent=True)
        try:
            result = self.builds.pause(self.build_id)
            self.assertEqual(result["pause_state"], "pausing-at-checkpoint")
            self.assertTrue(
                build_controls.control(self.builds, self.build_id)["pause_requested"]
            )
        finally:
            release.set()
            thread.join()

    def test_pause_and_deadline_survive_service_reopen(self):
        self.builds.pause(self.build_id)
        original = self.runtime.record(self.build_id)["deadline_monotonic"]
        self.runtime.close()
        profiles = self.profile.executor_profiles() + [self.profile.setup_profile()]
        self.runtime = Runtime(Path(self.temporary.name), profiles)
        self.builds = self.runtime.dependency_builds
        self.assertTrue(self.builds.status(self.build_id)["paused"])
        self.assertEqual(
            self.runtime.record(self.build_id)["deadline_monotonic"], original
        )

    def test_expiry_while_paused_cancels_original_build_without_start(self):
        self.builds.pause(self.build_id)
        operation = self.runtime.record(self.build_id)
        operation["deadline_monotonic"] = 0
        self.runtime.save(operation)
        with patch.object(self.runtime.docker, "run") as engine:
            self.builds.enforce_controls()
        result = self.builds.status(self.build_id)
        self.assertTrue(result["deadline_expired"])
        self.assertEqual(result["state"], "canceled")
        self.assertEqual(self.runtime.record(self.build_id)["state"], "exited")
        engine.assert_not_called()

    def test_later_pause_cannot_be_erased_by_resume_completion(self):
        self.builds.pause(self.build_id)

        def interleaved(_operation):
            self.builds.pause(self.build_id)
            return {"state": "not-started"}

        with (
            patch.object(self.runtime, "resume", side_effect=interleaved),
            patch.object(self.builds, "start") as start,
        ):
            result = self.builds.resume(self.build_id)
        self.assertTrue(result["pause_requested"])
        self.assertFalse(
            build_controls.control(self.builds, self.build_id)["resume_authorized"]
        )
        start.assert_not_called()

    def test_resume_requires_original_parent_binding(self):
        self.builds.pause(self.build_id)
        with (
            patch(
                "vibearr_executor.qualification.binding",
                return_value={"host": "changed"},
            ),
            self.assertRaises(Conflict),
        ):
            self.builds.resume(self.build_id)
        self.assertTrue(build_controls.gated(self.builds, self.build_id))

    def test_purged_build_replays_without_restarting(self):
        self.cancel()
        first = self.builds.purge(
            self.build_id, self.request["run_id"], ["source", "logs"]
        )
        with patch.object(self.runtime.docker, "run") as engine:
            self.assertEqual(self.builds.prepare(self.request), first)
            self.assertEqual(self.builds.start(self.build_id), first)
            self.assertEqual(self.builds.stop(self.build_id), first)
        self.assertTrue(first["tombstone"])
        self.assertEqual(first["state"], "purged")
        engine.assert_not_called()

    def test_build_purge_rejects_foreign_run(self):
        self.cancel()
        with self.assertRaises(Conflict):
            self.builds.purge(self.build_id, str(uuid4()), ["source", "logs"])
        self.assertTrue((self.builds.path(self.build_id) / "requirements.txt").exists())

    def test_active_and_unknown_builds_cannot_be_purged(self):
        for state in ("prepared", "setup", "assembling", "qualifying", "unknown"):
            with self.subTest(state=state):
                self.set_state(state)
                with self.assertRaises(Conflict):
                    self.builds.purge(
                        self.build_id, self.request["run_id"], ["source", "logs"]
                    )

    def test_full_build_purge_restores_admission_capacity(self):
        requests = [self.request]
        for _ in range(31):
            request = {**self.request, "build_id": str(uuid4())}
            self.builds.prepare(request)
            requests.append(request)
        candidate = {**self.request, "build_id": str(uuid4())}
        with self.assertRaises(Rejected):
            self.builds.prepare(candidate)
        self.cancel()
        self.builds.purge(self.build_id, self.request["run_id"], ["source"])
        with self.assertRaises(Rejected):
            self.builds.prepare(candidate)
        self.builds.purge(self.build_id, self.request["run_id"], ["logs"])
        self.assertEqual(self.builds.prepare(candidate)["state"], "prepared")

    def test_operator_registered_image_is_protected_from_build_purge(self):
        self.cancel()
        image = "sha256:" + "c" * 64
        self.set_state(
            "ready", image=image, image_commit_intent=True, dependency_digest="d" * 64
        )
        original = self.profile.executor_profiles()[0]
        operator = Profile.parse(
            {**original.data(), "profile_id": "operator-retained", "image": image}
        )
        self.runtime.profiles[operator.profile_id] = operator
        with (
            patch.object(self.builds, "committed_image", return_value=image),
            patch.object(self.runtime.docker, "run") as engine,
            self.assertRaises(Conflict),
        ):
            self.builds.purge(self.build_id, self.request["run_id"], ["source", "logs"])
        engine.assert_not_called()

    def test_retained_operation_reference_protects_derived_image(self):
        self.cancel()
        image = "sha256:" + "c" * 64
        self.set_state(
            "ready", image=image, image_commit_intent=True, dependency_digest="d" * 64
        )
        operation = self.runtime.record(self.build_id)
        operation["profile"]["image"] = image
        self.runtime.save(operation)
        with (
            patch.object(self.builds, "committed_image", return_value=image),
            patch.object(self.runtime.docker, "run") as engine,
            self.assertRaises(Conflict),
        ):
            self.builds.purge(self.build_id, self.request["run_id"], ["source", "logs"])
        engine.assert_not_called()
