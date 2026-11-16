from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from vibearr_runner.journal import Journal
from vibearr_runner import queue_acceptance


class QueueAcceptanceTests(unittest.TestCase):
    def test_progress_file_cannot_replace_the_controller_journal(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            journal = directory / "journal.json"
            journal.write_text("original controller state")
            with self.assertRaisesRegex(ValueError, "reserved"):
                queue_acceptance.run_acceptance(
                    directory,
                    1,
                    3,
                    directory / "receipt.json",
                    60,
                    progress_name="journal.json",
                )
            self.assertEqual(journal.read_text(), "original controller state")

    def test_interrupted_cycle_keeps_bounded_private_redacted_diagnostics(self):
        import json

        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            with patch.dict(
                "os.environ",
                {"VIBEARR_RUNNER_API_TOKEN": "sensitive-test-runner-token"},
            ):
                queue_acceptance.preserve_interruption(
                    directory,
                    "cycle output",
                    "x" * 70000 + " sensitive-test-runner-token",
                    1,
                )
            path = directory / "queue-acceptance-last-interruption.json"
            record = json.loads(path.read_text())
            self.assertNotIn("sensitive-test-runner-token", path.read_text())
            self.assertLessEqual(len(record["stderr"]), 65536)
            self.assertIn("[redacted]", record["stderr"])
            self.assertEqual(path.stat().st_mode & 0o077, 0)
            self.assertIn("original operations", record["resume"])

    def test_missing_explicit_execution_flag_never_constructs_a_queue_client(self):
        arguments = [
            "queue_acceptance",
            "--state",
            "/tmp/unused-acceptance-state",
            "--project-id",
            "1",
            "--issue-number",
            "3",
            "--evidence",
            "/tmp/unused-acceptance-evidence",
        ]
        with (
            patch("sys.argv", arguments),
            patch.object(queue_acceptance, "client") as client,
        ):
            with self.assertRaises(SystemExit):
                queue_acceptance.main()
        client.assert_not_called()

    def test_mismatched_owned_issue_is_stopped_before_a_controller_cycle(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "journal"
            with Journal(directory) as journal:
                state = journal.load()
                state["run"] = {
                    "run": {"state": "active"},
                    "item": {"project": {"id": 1}, "issue_number": 7},
                }
                journal.save(state)
            controller = SimpleNamespace(
                probe=Mock(return_value={"projects": [{"project_id": 1}]}),
                cycle=Mock(),
                suspend_owned=Mock(),
            )
            with (
                patch.object(
                    queue_acceptance, "client", return_value=SimpleNamespace()
                ),
                patch.object(queue_acceptance, "Controller", return_value=controller),
            ):
                with self.assertRaisesRegex(ValueError, "selected acceptance issue"):
                    queue_acceptance.cycle_once(directory, 1, 8)
            controller.cycle.assert_not_called()
            self.assertTrue(controller.suspend_owned.call_args.kwargs["ownership_lost"])

    def test_summary_keeps_server_project_shape_and_restart_checkpoint(self):
        state = {
            "run": {
                "run": {"id": "run", "state": "active"},
                "item": {"project": {"id": 2}, "issue_number": 9},
            },
            "native": {"phase": "delivery", "delivery": {"checkpoint": "pr_verified"}},
        }
        result = queue_acceptance.summary(state, "continue")
        self.assertEqual(result["project_id"], 2)
        self.assertEqual(result["issue_number"], 9)
        self.assertEqual(result["waypoint"], "pr-verified")
