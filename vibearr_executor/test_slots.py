from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from .artifacts import pack
from .contracts import Conflict, Limits, Profile, Rejected, digest
from .slots import SlotRuntime


class SlotManifestTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.profile = Profile(
            "fixed",
            1,
            "sha256:" + "a" * 64,
            "validation",
            ("/trusted/check",),
            Limits(2, 4096, 128, 2048),
        )
        with (
            patch("vibearr_executor.slots.terminate_children"),
            patch.object(SlotRuntime, "reset"),
        ):
            self.runtime = SlotRuntime(Path(self.temporary.name), [self.profile])
        self.addCleanup(self.runtime.close)
        source = pack({"test.txt": (0o644, b"candidate")})
        context = b"context"
        for kind, value in [("source", source), ("context", context)]:
            self.runtime.upload(kind, value, digest(value))
        self.manifest = {
            "schema_version": 1,
            "run_id": str(uuid4()),
            "operation_id": str(uuid4()),
            "profile_id": self.profile.profile_id,
            "profile_digest": self.profile.digest,
            "source_digest": digest(source),
            "context_digest": digest(context),
            "phase": "validation",
            "limits": self.profile.limits.__dict__,
            "network_hosts": [],
            "deadline": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
        }

    def test_lower_requested_memory_is_rejected_instead_of_falsely_reported(self):
        self.manifest["limits"] = replace(self.profile.limits, memory_mb=3072).__dict__
        with self.assertRaisesRegex(Rejected, "fixed slot"):
            self.runtime.prepare(self.manifest)

    def test_operation_identity_cannot_be_rebound(self):
        self.runtime.prepare(self.manifest)
        self.manifest["run_id"] = str(uuid4())
        with self.assertRaises(Conflict):
            self.runtime.prepare(self.manifest)

    def test_repeated_prepare_returns_the_same_saved_operation(self):
        first = self.runtime.prepare(self.manifest)
        self.assertEqual(self.runtime.prepare(self.manifest), first)

    def test_repeated_upload_is_available_when_artifact_storage_is_full(self):
        with (self.runtime.artifacts / "capacity-fixture").open("wb") as stream:
            stream.truncate(512 * 1024**2)
        repeated = self.runtime.upload("context", b"context", digest(b"context"))
        self.assertEqual(repeated["sha256"], self.manifest["context_digest"])

    def test_validation_rejects_backend_credentials(self):
        self.runtime.prepare(self.manifest)
        with self.assertRaisesRegex(Rejected, "agent operation"):
            self.runtime.supply_secret(
                self.manifest["operation_id"], "private-model-key"
            )

    def test_start_requires_current_boot_qualification(self):
        self.runtime.prepare(self.manifest)
        with patch.object(
            self.runtime, "capabilities", return_value={"qualified": False}
        ):
            with self.assertRaisesRegex(Rejected, "Qualify"):
                self.runtime.start(self.manifest["operation_id"])
        self.assertEqual(
            self.runtime.status(self.manifest["operation_id"])["state"], "not-started"
        )

    def test_active_or_prepared_data_cannot_be_removed_by_retention(self):
        self.runtime.prepare(self.manifest)
        with self.assertRaises(Conflict):
            self.runtime.purge(
                self.manifest["operation_id"], self.manifest["run_id"], ["source"]
            )

    def test_stop_of_prepared_work_never_launches_a_process(self):
        self.runtime.prepare(self.manifest)
        status = self.runtime.stop(self.manifest["operation_id"])
        self.assertEqual(status["state"], "exited")
        self.assertTrue(status["descendants_terminated"])
        self.assertIsNone(self.runtime.process)

    def test_task_cannot_select_a_command_in_its_manifest(self):
        self.manifest["argv"] = ["untrusted"]
        with self.assertRaisesRegex(Rejected, "commands and mounts"):
            self.runtime.prepare(self.manifest)
