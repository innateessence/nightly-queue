from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import unittest
from uuid import uuid4

from vibearr_executor.contracts import Profile
from vibearr_executor.integrated_profiles import LIMITS, MODEL_HOSTS
from vibearr_executor.slot_qualification import ARGV

from .compact_evidence import digest, validate_payload


class ArchivedRestartProofTests(unittest.TestCase):
    def setUp(self):
        self.ended = datetime(2024, 1, 1, tzinfo=timezone.utc)
        self.worker = {
            "runtime": "compact-worker-v1",
            "binding_digest": "b" * 64,
            "instance_id": str(uuid4()),
            "worker_boot": str(uuid4()),
            "image_digest": "sha256:" + "a" * 64,
        }
        identity = {**self.worker, "worker_boot": str(uuid4())}
        profile = Profile(
            "worker-qualification",
            1,
            self.worker["image_digest"],
            "agent",
            ARGV,
            LIMITS,
            MODEL_HOSTS,
            "",
            self.worker["image_digest"],
        )
        operation, run = str(uuid4()), str(uuid4())
        self.record = {
            "operation_id": operation,
            "state": "exited",
            "descendants_terminated": True,
            "worker_boot": identity["worker_boot"],
            "execution_identity": identity,
            "manifest": {
                "schema_version": 1,
                "operation_id": operation,
                "run_id": run,
                "profile_id": profile.profile_id,
                "profile_digest": profile.digest,
                "source_digest": "c" * 64,
                "context_digest": digest({"case": "wait"}),
                "phase": "agent",
                "limits": asdict(LIMITS),
                "deadline": (self.ended + timedelta(seconds=180)).isoformat(),
                "network_hosts": list(MODEL_HOSTS),
            },
            "result": {
                "operation_id": operation,
                "run_id": run,
                "execution_identity": identity,
                "exit_code": None,
                "worker_restart": True,
                "reconciled_boot": self.worker["worker_boot"],
                "terminated": True,
                "timed_out": False,
                "started_at": None,
                "ended_at": self.ended.timestamp(),
            },
        }

    def validate(self):
        self.record["receipt_digest"] = digest(
            {
                "manifest": self.record["manifest"],
                "result": self.record["result"],
                "boot": self.record["worker_boot"],
            }
        )
        boundary = {"restart_receipt": self.record["receipt_digest"]}
        self.worker["qualification_digest"] = digest(boundary)
        payload = {
            "operation": self.record,
            "restart_receipt": self.record["receipt_digest"],
            "boundary": boundary,
        }
        validate_payload("worker_restart", payload, "c" * 40, self.worker, {})

    def test_expired_restart_uses_only_its_sealed_reconciliation_observation(self):
        self.validate()
        self.assertIsNone(self.record["result"]["started_at"])

    def test_reconciliation_after_original_deadline_is_rejected(self):
        self.record["result"]["ended_at"] = (
            self.ended + timedelta(seconds=181)
        ).timestamp()
        with self.assertRaisesRegex(ValueError, "next hour"):
            self.validate()

    def test_missing_reconciliation_observation_is_rejected(self):
        self.record["result"].pop("ended_at")
        with self.assertRaisesRegex(ValueError, "sealed observation"):
            self.validate()

    def test_boolean_reconciliation_observation_is_rejected(self):
        self.record["result"]["ended_at"] = True
        with self.assertRaisesRegex(ValueError, "sealed observation"):
            self.validate()

    def test_nonfinite_reconciliation_observation_cannot_be_sealed(self):
        self.record["result"]["ended_at"] = float("nan")
        with self.assertRaises(ValueError):
            self.validate()

    def test_restart_with_fabricated_start_remains_rejected(self):
        self.record["result"]["started_at"] = self.ended.timestamp()
        with self.assertRaises(ValueError):
            self.validate()
