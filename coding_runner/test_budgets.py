import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

from .budgets import ActiveBudget, BudgetExceeded
from .journal import Journal


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class BudgetTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.run_id = str(uuid4())
        self.journal = Journal(self.root / "journal")
        self.journal.__enter__()
        self.addCleanup(self.journal.__exit__, None, None, None)
        self.state = self.journal.load()
        self.clock = Clock()
        self.limits = {"research": 10, "implementation": 10, "validation": 10}
        self.budget = ActiveBudget(
            self.journal,
            self.state,
            self.run_id,
            15,
            self.limits,
            clock=self.clock,
        )

    def test_total_budget_survives_stage_transition_and_restart(self):
        self.budget.run("research", lambda remaining: setattr(self.clock, "now", 8))
        self.budget.run(
            "implementation", lambda remaining: setattr(self.clock, "now", 14)
        )
        restored = ActiveBudget(
            self.journal,
            self.journal.load(),
            self.run_id,
            15,
            self.limits,
            clock=self.clock,
        )
        self.assertEqual(restored.remaining("validation"), 1)

    def test_stage_budget_limits_process_timeout(self):
        remaining = self.budget.run("validation", lambda remaining: remaining)
        self.assertEqual(remaining, 10)

    def test_unresolved_active_segment_blocks_replay(self):
        self.budget.data["active"] = {"stage": "research", "started": 1}
        self.journal.save(self.state)
        with self.assertRaisesRegex(BudgetExceeded, "recovery"):
            self.budget.remaining("research")

    def test_exhaustion_records_elapsed_even_when_operation_raises(self):
        def expired(remaining):
            self.clock.now += 11
            raise ValueError("Failed execution")

        with self.assertRaises(ValueError):
            self.budget.run("research", expired)
        self.assertEqual(self.budget.data["total"], 11)
        self.assertIsNone(self.budget.data["active"])
        self.assertEqual(self.budget.remaining("research"), 0)
