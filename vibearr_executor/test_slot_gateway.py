from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

from .client import ExecutorUnavailable
from .contracts import Conflict, Rejected
from .slot_gateway import Gateway


class Worker:
    def __init__(self):
        self.instance = str(uuid4())
        self.calls = []

    def capabilities(self):
        return {
            "runtime": "compose-slot-v2",
            "protocol_version": 2,
            "instance_id": self.instance,
            "profiles": [{"profile_id": "fixed", "profile_digest": "a" * 64}],
        }

    def prepare(self, manifest):
        self.calls.append(manifest)
        return {"state": "prepared", "operation_id": manifest["operation_id"]}


class GatewayRoutingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.worker = Worker()
        self.gateway = Gateway(Path(self.temporary.name), {"validation": self.worker})
        self.addCleanup(self.gateway.close)
        self.manifest = {
            "operation_id": str(uuid4()),
            "phase": "validation",
            "profile_id": "fixed",
            "profile_digest": "a" * 64,
        }

    def test_retry_preserves_the_assigned_worker(self):
        first = self.gateway.prepare(self.manifest)
        self.assertEqual(self.gateway.prepare(self.manifest), first)
        self.assertEqual(len(self.worker.calls), 2)

    def test_worker_volume_replacement_fences_an_existing_operation(self):
        self.gateway.prepare(self.manifest)
        self.worker.instance = str(uuid4())
        with self.assertRaises(ExecutorUnavailable):
            self.gateway.prepare(self.manifest)
        self.assertEqual(len(self.worker.calls), 1)

    def test_an_operation_cannot_change_its_recipe_after_assignment(self):
        self.gateway.prepare(self.manifest)
        self.manifest["profile_digest"] = "b" * 64
        with self.assertRaises(Conflict):
            self.gateway.prepare(self.manifest)

    def test_unknown_phase_is_rejected(self):
        self.manifest["phase"] = "host"
        with self.assertRaises(Rejected):
            self.gateway.prepare(self.manifest)
