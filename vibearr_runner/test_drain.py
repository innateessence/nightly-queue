from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from vibearr_runner.controller import Controller
from vibearr_runner.drain import acknowledge, change, requested, status
from vibearr_runner.journal import Journal


class DrainTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.directory.chmod(0o700)

    def test_repeated_request_preserves_the_drain_generation(self):
        change(self.directory, True)
        identity = requested(self.directory)
        change(self.directory, True)
        self.assertEqual(requested(self.directory), identity)

    def test_pending_claim_cannot_acknowledge_a_drain(self):
        change(self.directory, True)
        with Journal(self.directory) as journal:
            state = {"version": 1, "operation": {"kind": "claim"}}
            acknowledge(journal, state)
            self.assertNotIn("drain_ack", state)

    def test_old_acknowledgement_cannot_satisfy_a_new_drain(self):
        change(self.directory, True)
        with Journal(self.directory) as journal:
            state = {"version": 1, "operation": None}
            acknowledge(journal, state)
        self.assertTrue(status(self.directory)["drained"])
        change(self.directory, False)
        change(self.directory, True)
        self.assertFalse(status(self.directory)["drained"])

    def test_drained_controller_does_not_request_new_work(self):
        change(self.directory, True)
        client = Mock()
        with Journal(self.directory) as journal:
            controller = Controller(client, journal, lifecycle_factory=Mock())
            with patch("vibearr_runner.controller.RecoveryCoordinator"):
                result = controller.cycle({"version": 1, "operation": None}, {})
        self.assertEqual(result, "drained")
        client.request.assert_not_called()
        self.assertTrue(status(self.directory)["drained"])

    def test_owned_work_continues_while_new_claims_are_draining(self):
        change(self.directory, True)
        lifecycle = Mock()
        lifecycle.step.return_value = "owned-progress"
        state = {"version": 1, "operation": None, "run": {"run": {"state": "active"}}}
        with Journal(self.directory) as journal:
            controller = Controller(
                Mock(), journal, lifecycle_factory=lambda *args, **kwargs: lifecycle
            )
            with patch("vibearr_runner.controller.RecoveryCoordinator"):
                result = controller.cycle(state, {})
        self.assertEqual(result, "owned-progress")
        lifecycle.step.assert_called_once_with(state)
        self.assertNotIn("drain_ack", state)
