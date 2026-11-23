import unittest

from .compact_proof_export import oom_operation
from .contracts import canonical, digest


class OOMProofSelectionTests(unittest.TestCase):
    def record(self, case):
        return {
            "manifest": {
                "profile_id": "worker-qualification",
                "phase": "agent",
                "context_digest": digest(canonical({"case": case})),
            },
            "result": {"exit_code": -9},
        }

    def test_memory_probe_is_selected_among_other_killed_qualification_tasks(self):
        memory = self.record("memory")
        records = [
            self.record("wait"),
            self.record("wait"),
            self.record("pids"),
            memory,
            self.record("flood"),
        ]
        self.assertIs(oom_operation(records), memory)

    def test_killed_tasks_without_the_fixed_memory_probe_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "exactly one"):
            oom_operation([self.record("wait"), self.record("pids")])

    def test_duplicate_memory_probes_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "exactly one"):
            oom_operation([self.record("memory"), self.record("memory")])

    def test_other_profile_cannot_supply_a_memory_probe(self):
        record = self.record("memory")
        record["manifest"]["profile_id"] = "unregistered"
        with self.assertRaisesRegex(ValueError, "exactly one"):
            oom_operation([record])

    def test_other_phase_cannot_supply_a_memory_probe(self):
        record = self.record("memory")
        record["manifest"]["phase"] = "validation"
        with self.assertRaisesRegex(ValueError, "exactly one"):
            oom_operation([record])
