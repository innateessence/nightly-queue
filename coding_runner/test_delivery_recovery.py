import copy
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from vibearr_runner.delivery_recovery import restore
from vibearr_runner.journal import JournalError
from vibearr_runner.merge_contract import completion_marker
from vibearr_runner.native_revision import NativeRevision
from vibearr_runner.delivery import DeliveryError
from vibearr_runner.workspaces import Workspace


class DeliveryRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.run_id = str(uuid4())
        self.expected = {
            "expected_finish_digest": "d" * 64,
            "expected_publication_id": str(uuid4()),
            "expected_head_sha": "a" * 40,
        }
        self.state = {
            "runner_id": 2,
            "gates": {
                "counts": {"day": 3},
                "attempts": {},
                "checkpoint": {
                    "run_id": self.run_id,
                    "interval": {"id": "day"},
                    "readings": [],
                },
            },
            "completion_receipts": [{"run_id": self.run_id, "finish_digest": "d" * 64}],
        }
        self.workspace = Workspace(
            self.run_id,
            "https://github.com/owner/repo.git",
            "main",
            "main",
            "b" * 40,
            "task",
            Path("/tmp/retained"),
        )
        self.source = SimpleNamespace(
            commit_sha="a" * 40, tree_sha="c" * 40, digest="e" * 64
        )
        self.journal = SimpleNamespace(directory=Path("/tmp/private"), save=Mock())
        self.queue = Mock()

        def request(method, path, payload=None):
            run = {"id": self.run_id, "runner_id": 2, "state": "active"}
            if method == "GET":
                return {"run": run}
            return {
                "run": run,
                "item": {"claim_token": "new"},
                "delivery_recovery": {
                    "operation_id": payload["operation_id"],
                    "generation": 1,
                    "archive": {"native_outcome": "failed"},
                    "native": {
                        "run_id": self.run_id,
                        "delivery_recovery_generation": 1,
                        "repository": {"repository_url": self.workspace.repository_url},
                        "candidate": {
                            "source_digest": "e" * 64,
                            "workspace": {
                                "commit_sha": "a" * 40,
                                "tree_sha": "c" * 40,
                                "branch": "task",
                            },
                        },
                        "delivery": {
                            "publication_id": self.expected["expected_publication_id"]
                        },
                    },
                },
            }

        self.queue.request.side_effect = request
        self.retained = patch(
            "vibearr_runner.delivery_recovery.retained_workspace",
            return_value=(self.workspace, self.source),
        )
        self.retained.start()
        self.addCleanup(self.retained.stop)

    def test_restore_keeps_count_and_failed_ack_without_new_paid_operation(self):
        result = restore(
            self.queue,
            self.journal,
            self.state,
            run_id=self.run_id,
            expected=self.expected,
            execute=True,
        )
        self.assertTrue(result["restored"])
        self.assertEqual(self.state["gates"]["counts"], {"day": 3})
        self.assertEqual(self.state["gates"]["attempts"][self.run_id], "day")
        self.assertEqual(
            self.state["completion_receipts"][0]["finish_digest"], "d" * 64
        )
        self.assertEqual(
            [c.args[0] for c in self.queue.request.call_args_list], ["GET", "POST"]
        )

    def test_lost_response_reuses_original_recovery_request(self):
        request = self.queue.request.side_effect
        self.queue.request.side_effect = [request("GET", ""), RuntimeError("lost")]
        with self.assertRaises(RuntimeError):
            restore(
                self.queue,
                self.journal,
                self.state,
                run_id=self.run_id,
                expected=self.expected,
                execute=True,
            )
        operation = self.state["delivery_recovery_request"]["operation_id"]
        self.queue.request.side_effect = request
        restore(
            self.queue,
            self.journal,
            self.state,
            run_id=self.run_id,
            expected=self.expected,
            execute=True,
        )
        self.assertEqual(
            self.state["delivery_recoveries"][self.run_id]["operation_id"], operation
        )

    def test_preview_does_not_reserve_or_activate_recovery(self):
        before = copy.deepcopy(self.state)
        restore(
            self.queue,
            self.journal,
            self.state,
            run_id=self.run_id,
            expected=self.expected,
        )
        self.assertEqual(self.state, before)
        self.journal.save.assert_not_called()
        self.assertEqual(self.queue.request.call_count, 1)

    def test_missing_original_ack_fences_before_api(self):
        self.state["completion_receipts"] = []
        with self.assertRaises(JournalError):
            restore(
                self.queue,
                self.journal,
                self.state,
                run_id=self.run_id,
                expected=self.expected,
                execute=True,
            )
        self.queue.request.assert_not_called()

    def test_existing_paid_operation_fences_recovery(self):
        self.state["native"] = {"run_id": self.run_id}
        with self.assertRaises(JournalError):
            restore(
                self.queue,
                self.journal,
                self.state,
                run_id=self.run_id,
                expected=self.expected,
                execute=True,
            )
        self.queue.request.assert_not_called()

    def test_delivery_recovery_cannot_start_ci_model_repair(self):
        host = SimpleNamespace(native={"delivery_recovery_generation": 1})
        with self.assertRaisesRegex(DeliveryError, "cannot run a model repair"):
            NativeRevision(host).prepare("ci_repair", "failed required check")

    def test_completion_marker_keeps_original_failure_separate(self):
        self.assertEqual(
            completion_marker(self.run_id, 1),
            f"<!-- vibearr-run:{self.run_id}:delivery-recovery:1:complete -->",
        )
        self.assertNotEqual(
            completion_marker(self.run_id), completion_marker(self.run_id, 1)
        )
