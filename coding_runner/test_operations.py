import fcntl
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

from .client import InvalidResponse
from .journal import Journal
from .maintenance import Maintenance, MINIMUM_FREE_BYTES
from .release import ControlledRelease, matches_deployment
from .run_logs import RunLogs


class MaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.directory = self.root / "state"
        self.directory.mkdir(mode=0o700)
        self.journal = Journal(self.directory)
        self.journal.__enter__()
        self.addCleanup(self.journal.__exit__)
        self.state = self.journal.load()
        self.run_id = str(uuid4())
        self.workspaces = self.directory / "workspaces"
        self.workspaces.mkdir(mode=0o700)
        self.client = Mock()
        self.client.request.return_value = {
            "schema_version": 1,
            "workspaces": [self.run_id],
            "logs": [],
        }
        self.maintenance = Maintenance(
            self.client,
            self.journal,
            self.workspaces,
            disk_usage=lambda path: SimpleNamespace(free=MINIMUM_FREE_BYTES + 1),
        )

    def workspace(self):
        directory = self.workspaces / self.run_id
        directory.mkdir(mode=0o700)
        (directory / "changes.txt").write_text("recoverable")
        return directory

    def test_eligible_workspace_is_removed_and_acknowledged(self):
        directory = self.workspace()
        self.assertTrue(self.maintenance(self.state, {}))
        self.assertFalse(directory.exists())
        self.assertEqual(self.state["maintenance"]["removed"], 1)

    def test_remote_active_workspace_is_never_removed(self):
        directory = self.workspace()
        self.state["remote_active_run_id"] = self.run_id
        self.maintenance(self.state, {})
        self.assertTrue(directory.exists())

    def test_pending_operation_workspace_is_never_removed(self):
        directory = self.workspace()
        self.state["operation"] = {"run_id": self.run_id}
        self.maintenance(self.state, {})
        self.assertTrue(directory.exists())

    def test_locked_workspace_is_preserved(self):
        directory = self.workspace()
        with (directory / "workspace.lock").open("w") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.maintenance(self.state, {})
        self.assertTrue(directory.exists())
        self.assertEqual(self.state["maintenance"]["busy"], 1)

    def test_missing_volume_workspace_becomes_history_tombstone(self):
        self.maintenance(self.state, {})
        self.assertEqual(self.state["maintenance"]["missing"], 1)

    def test_symbolic_link_cannot_redirect_retention(self):
        outside = self.root / "outside"
        outside.mkdir()
        (self.workspaces / self.run_id).symlink_to(outside)
        with self.assertRaises(InvalidResponse):
            self.maintenance(self.state, {})
        self.assertTrue(outside.exists())

    def test_invalid_server_path_is_rejected(self):
        self.client.request.return_value["workspaces"] = ["../outside"]
        with self.assertRaises(ValueError):
            self.maintenance(self.state, {})

    def test_disk_pressure_blocks_new_claims_without_deleting_protected_work(self):
        directory = self.workspace()
        self.state["remote_active_run_id"] = self.run_id
        self.maintenance.disk_usage = lambda path: SimpleNamespace(free=1)
        self.assertFalse(self.maintenance(self.state, {}))
        self.assertTrue(directory.exists())
        self.assertIn("Disk pressure", self.state["maintenance"]["blocking_reason"])

    def test_retention_never_reaches_unlisted_workspace(self):
        directory = self.workspace()
        self.client.request.return_value["workspaces"] = []
        self.maintenance(self.state, {})
        self.assertTrue(directory.exists())

    def test_acknowledgement_compacts_bulk_state_after_durable_confirmation(self):
        other = str(uuid4())
        for key in ("research", "implementation", "budgets"):
            self.state[key] = {
                self.run_id: {"large": "x" * 1000000},
                other: {"active": True},
            }
        self.state["gates"] = {
            "attempts": {self.run_id: "current", other: "current"},
            "counts": {"current": 2, "old": 9},
            "interval_ends": {
                "current": "2999-01-01T00:00:00+00:00",
                "old": "2000-01-01T00:00:00+00:00",
            },
            "pending_claim": {"run_id": other, "interval_id": "current"},
        }
        self.maintenance.acknowledge(self.state, self.run_id, str(uuid4()), "f" * 64)
        recovered = self.journal.load()
        self.assertNotIn(self.run_id, recovered["research"])
        self.assertTrue(recovered["research"][other]["active"])
        self.assertEqual(recovered["gates"]["counts"], {"current": 2})
        self.assertEqual(recovered["gates"]["pending_claim"]["run_id"], other)
        self.assertLess(self.journal.path.stat().st_size, 10000)

    def test_failed_acknowledgement_preserves_bulk_recovery_state(self):
        self.state["research"] = {self.run_id: {"pending_model": "preserve"}}
        self.client.request.side_effect = InvalidResponse("Lost acknowledgement")
        with self.assertRaises(InvalidResponse):
            self.maintenance.acknowledge(
                self.state, self.run_id, str(uuid4()), "f" * 64
            )
        self.assertEqual(
            self.state["research"][self.run_id]["pending_model"], "preserve"
        )

    def test_raw_log_is_private_and_only_summary_is_published(self):
        run_id, execution_id = str(uuid4()), str(uuid4())
        published = []
        logs = RunLogs(self.directory / "logs", published.append)
        action = SimpleNamespace(to_data=lambda: {"argv": ["test"]})
        workspace = SimpleNamespace(workspace_id=run_id, base_revision="a" * 40)
        result = SimpleNamespace(
            execution_id=execution_id,
            output=b"private raw content",
            exit_code=2,
            interrupted=False,
        )
        logs(workspace, action, result)
        path = self.directory / "logs" / run_id / (execution_id + ".log")
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(path.read_bytes(), b"private raw content")
        self.assertNotIn("private raw content", json.dumps(published))
        self.assertEqual(published[0]["exit_code"], 2)


class ControlledReleaseTests(unittest.TestCase):
    def test_missing_qualification_does_not_probe_or_grant_execution(self):
        self.assertFalse(matches_deployment({"qualified": False}))

    def test_replay_mode_is_persisted_as_replay(self):
        client, journal = Mock(), Mock()
        binding = {"code_revision": "a" * 64, "environment_digest": "b" * 64}
        client.request.return_value = {
            "schema_version": 1,
            "qualification": {**binding, "expires_at": "2999-01-01T00:00:00+00:00"},
        }
        harness = ControlledRelease(
            client, journal, str(uuid4()), mode="replay", binding=lambda: binding
        )
        harness.submit({"success_run_id": str(uuid4())})
        self.assertEqual(client.request.call_args.args[2]["report"]["mode"], "replay")

    def test_changed_deployment_cannot_run_authorized_scenario(self):
        client = Mock()
        client.request.return_value = {
            "schema_version": 1,
            "qualification": {"code_revision": "a" * 64},
        }
        harness = ControlledRelease(
            client, Mock(), str(uuid4()), binding=lambda: {"code_revision": "b" * 64}
        )
        with self.assertRaises(InvalidResponse):
            harness.authorization()

    def test_wrong_repository_cannot_enter_lifecycle(self):
        client = Mock()
        client.request.return_value = {
            "schema_version": 1,
            "qualification": {
                "repository_url": "https://github.com/example/test",
                "expires_at": "2999-01-01T00:00:00+00:00",
            },
        }
        harness = ControlledRelease(client, Mock(), str(uuid4()), binding=lambda: {})
        lifecycle = Mock()
        with self.assertRaises(InvalidResponse):
            harness.step(
                {
                    "run": {
                        "item": {
                            "repository_url": "https://github.com/example/production"
                        }
                    }
                },
                lifecycle,
            )
        lifecycle.step.assert_not_called()
