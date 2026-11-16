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
