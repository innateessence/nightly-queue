import unittest
from unittest.mock import Mock, patch

from .client import NotFoundError
from .context import ContextError, ContextInterrupted
from .recovery import DurableClient


class RecoveryDeadlineTests(unittest.TestCase):
    def setUp(self):
        self.client = Mock()
        self.journal = Mock()
        self.pending = {
            "path": "/api/v1/runner/runs/owned-run/contexts/",
            "payload": {
                "operation_id": "owned-operation",
                "document": {"title": "Frozen task"},
            },
            "status": "pending",
        }
        self.state = {"request": self.pending}
        self.durable = DurableClient(self.client, self.journal, self.state)

    def test_expired_budget_prevents_recovery_lookup(self):
        with patch("vibearr_runner.context.time.monotonic", return_value=10):
            with self.assertRaisesRegex(ContextError, "budget"):
                self.durable.recover(timeout=0)
        self.client.request.assert_not_called()
        self.journal.save.assert_not_called()
        self.assertEqual(self.pending["status"], "pending")

    def test_nonfinite_budget_prevents_requests(self):
        for timeout in (float("inf"), float("-inf"), float("nan")):
            with self.subTest(timeout=timeout), self.assertRaises(ContextError):
                self.durable.request("GET", "/api/v1/runner/identity/", timeout=timeout)
        self.client.request.assert_not_called()

    def test_stop_prevents_unbounded_recovery_lookup(self):
        with self.assertRaises(ContextInterrupted):
            self.durable.recover(checkpoint=lambda: False)
        self.client.request.assert_not_called()
        self.journal.save.assert_not_called()
        self.assertEqual(self.pending["status"], "pending")

    def test_lookup_exhausting_budget_cannot_replay_metadata(self):
        clock = [10.0]

        def unavailable(*args, **kwargs):
            clock[0] = 12.0
            raise NotFoundError("Original operation is absent")

        self.client.request.side_effect = unavailable
        with patch(
            "vibearr_runner.context.time.monotonic", side_effect=lambda: clock[0]
        ):
            with self.assertRaisesRegex(ContextError, "budget"):
                self.durable.recover(timeout=1)
        self.client.request.assert_called_once()
        self.assertEqual(self.client.request.call_args.args[0], "GET")
        self.journal.save.assert_not_called()
        self.assertEqual(self.pending["status"], "pending")
