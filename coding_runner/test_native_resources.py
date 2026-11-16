from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

from vibearr_runner.native_resources import NativeResources
from vibearr_runner.recovery import ReconciliationRequired
from vibearr_runner.workspaces import WorkspaceError


class Executor:
    def __init__(self):
        self.deleted = []
        self.instance_id = str(uuid4())

    def capabilities(self):
        return {"instance_id": self.instance_id}

    def purge(self, operation_id, **kwargs):
        self.deleted.append((operation_id, kwargs))

    def purge_dependencies(self, build_id, **kwargs):
        self.deleted.append((build_id, kwargs))


class NativeResourceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.store = NativeResources(Path(self.temporary.name))
        self.run = str(uuid4())
        self.operation = str(uuid4())
        self.manifest = {"run_id": self.run, "operation_id": self.operation}

    def tearDown(self):
        self.temporary.cleanup()

    def test_reference_cannot_be_rebound_to_another_operation(self):
        self.store.operation(self.run, self.manifest, kind="agent")
        with self.assertRaises(WorkspaceError):
            self.store.operation(
                self.run, {**self.manifest, "source_digest": "different"}, kind="agent"
            )

    def test_record_survives_workspace_removal_until_logs_expire(self):
        executor = Executor()
        self.store.bind(self.run, executor)
        self.store.operation(self.run, self.manifest, kind="agent")
        self.store.purge(self.run, executor, "workspace")
        self.assertTrue(self.store.path(self.run).exists())
        self.assertEqual(
            executor.deleted[0][1], {"run_id": self.run, "resources": ["source"]}
        )
        self.store.purge(self.run, executor, "logs")
        self.assertFalse(self.store.path(self.run).exists())
        self.assertEqual(
            executor.deleted[1][1], {"run_id": self.run, "resources": ["logs"]}
        )

    def test_other_run_reference_is_rejected(self):
        with self.assertRaises(WorkspaceError):
            self.store.operation(str(uuid4()), self.manifest, kind="agent")

    def test_reinitialized_executor_state_cannot_recreate_a_previous_operation(self):
        original = Executor()
        self.store.bind(self.run, original)
        self.store.operation(self.run, self.manifest, kind="agent")
        with self.assertRaisesRegex(ReconciliationRequired, "state was replaced"):
            self.store.reconcile(self.run, Executor())

    def test_same_state_restart_keeps_execution_identity(self):
        original = Executor()
        identity = self.store.bind(self.run, original)
        replacement = Executor()
        replacement.instance_id = identity
        self.assertEqual(self.store.bind(self.run, replacement), identity)

    def test_dispatched_record_loss_does_not_recreate_or_attest_termination(self):
        from unittest.mock import Mock

        executor = Executor()
        executor.prepare = Mock()
        executor.status = Mock(return_value={"state": "not-started"})
        self.store.bind(self.run, executor)
        self.store.operation(self.run, self.manifest, kind="agent")
        self.store.dispatch(self.run, self.operation)
        with self.assertRaisesRegex(ReconciliationRequired, "never recreate"):
            self.store.reconcile(self.run, executor, stop=True)
        executor.prepare.assert_not_called()

    def test_cancel_before_prepare_can_resolve_absence_without_dispatch(self):
        import importlib.util
        from unittest.mock import Mock

        if importlib.util.find_spec("vibearr_executor") is None:
            self.skipTest("Install the native-runtime extra for executor absence tests")
        from vibearr_executor.client import ExecutorRejected

        executor = Executor()
        executor.stop = Mock(
            side_effect=ExecutorRejected("never prepared", http_status=404)
        )
        self.store.bind(self.run, executor)
        self.store.reserve(self.run, self.operation)
        self.assertEqual(
            self.store.reconcile(self.run, executor, stop=True),
            {"operations": [], "builds": []},
        )
        self.assertEqual(self.store.load(self.run)["pending"], {})
