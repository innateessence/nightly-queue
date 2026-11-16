import copy
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4

from .qualify import Harness, memory_oom_observation, smoke_profiles


class MemoryProbeTests(unittest.TestCase):
    def setUp(self):
        self.profile = next(
            entry
            for entry in smoke_profiles("sha256:" + "a" * 64)
            if entry.profile_id == "smoke-oom-v1"
        )
        self.operation = str(uuid4())
        self.manifest = {
            "profile_id": self.profile.profile_id,
            "profile_digest": self.profile.digest,
            "operation_id": self.operation,
            "limits": asdict(self.profile.limits),
        }
        self.status = {
            "operation_id": self.operation,
            "container_id": "container-id",
            "state": "unknown",
        }
        self.inspected = {
            "Id": "container-id",
            "Image": self.profile.image,
            "Config": {"Labels": {"vibearr.operation": self.operation}},
            "State": {"Running": False, "OOMKilled": True, "ExitCode": 137},
            "HostConfig": {
                "Memory": 256 * 1024 * 1024,
                "MemorySwap": 256 * 1024 * 1024,
                "PidsLimit": 32,
            },
        }

    def test_exact_memory_probe_can_record_engine_attested_init_oom(self):
        observed = memory_oom_observation(
            self.profile, self.manifest, self.status, self.inspected
        )
        self.assertEqual(observed["outcome"], "container_oom")
        self.assertIsNone(observed["task_exit_code"])
        self.assertEqual(
            observed["workspace_export"], "unavailable_after_container_oom"
        )
        self.assertEqual(self.status["state"], "unknown")

    def test_arbitrary_signal_is_not_an_expected_oom(self):
        self.inspected["State"]["OOMKilled"] = False
        with self.assertRaisesRegex(RuntimeError, "not a confirmed"):
            memory_oom_observation(
                self.profile, self.manifest, self.status, self.inspected
            )

    def test_foreign_container_operation_or_image_is_rejected(self):
        for change in (
            {"Id": "foreign"},
            {"Image": "sha256:" + "b" * 64},
            {"Config": {"Labels": {"vibearr.operation": str(uuid4())}}},
        ):
            with self.subTest(change=change), self.assertRaises(RuntimeError):
                memory_oom_observation(
                    self.profile,
                    self.manifest,
                    self.status,
                    {**self.inspected, **change},
                )

    def test_changed_engine_memory_limit_is_rejected(self):
        self.inspected["HostConfig"]["Memory"] *= 2
        with self.assertRaisesRegex(RuntimeError, "engine limits"):
            memory_oom_observation(
                self.profile, self.manifest, self.status, self.inspected
            )

    def test_ordinary_profile_unknown_is_never_accepted_as_expected_oom(self):
        profile = next(
            entry
            for entry in smoke_profiles(self.profile.image)
            if entry.profile_id == "smoke-validation-v1"
        )
        manifest = {
            **self.manifest,
            "profile_id": profile.profile_id,
            "profile_digest": profile.digest,
        }
        with self.assertRaisesRegex(RuntimeError, "exact fixed"):
            memory_oom_observation(profile, manifest, self.status, self.inspected)

    def protected_status(self):
        return {
            **self.status,
            "state": "exited",
            "descendants_terminated": True,
            "result": {
                "exit_code": 0,
                "terminated": True,
                "resources": {
                    "memory.events": {"oom_kill": 1},
                    "memory.max": {"value": 256 * 1024 * 1024},
                    "memory.swap.max": {"value": 0},
                },
            },
        }

    def test_protected_oom_result_keeps_actual_exit_and_missing_export(self):
        observed = memory_oom_observation(
            self.profile, self.manifest, self.protected_status(), None
        )
        self.assertEqual(observed["task_exit_code"], 0)
        self.assertEqual(observed["workspace_export"], "unavailable")

    def test_protected_result_without_kernel_oom_counter_is_rejected(self):
        status = self.protected_status()
        status["result"]["resources"]["memory.events"]["oom_kill"] = 0
        with self.assertRaisesRegex(RuntimeError, "no protected OOM"):
            memory_oom_observation(self.profile, self.manifest, status, None)

    def test_finished_unknown_diagnostic_names_phase_and_excludes_unrelated_data(self):
        class Client:
            def status(inner, operation_id):
                return {
                    **self.status,
                    "wait_reason": "Container stopped",
                    "container_state": {"oom_killed": True},
                    "secret": "do-not-log",
                }

        with tempfile.TemporaryDirectory() as temporary:
            harness = Harness.__new__(Harness)
            harness.state = Path(temporary)
            harness.client = Client()
            harness.jobs = {self.operation: "smoke-validation-v1"}
            with self.assertRaisesRegex(RuntimeError, "smoke-validation-v1") as caught:
                harness.finished(self.operation)
            self.assertNotIn("do-not-log", str(caught.exception))
            stored = (Path(temporary) / "last-failure.json").read_text()
            self.assertIn("smoke-validation-v1", stored)
            self.assertNotIn("do-not-log", stored)

    def test_kernel_limit_and_cancellation_cannot_be_waived(self):
        for change in ({"canceled": True}, {"timed_out": True}, {"exit_code": 1}):
            status = copy.deepcopy(self.protected_status())
            status["result"].update(change)
            with self.subTest(change=change), self.assertRaises(RuntimeError):
                memory_oom_observation(self.profile, self.manifest, status, None)
