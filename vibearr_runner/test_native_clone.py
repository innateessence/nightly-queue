from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from vibearr_runner.journal import Journal
from vibearr_runner.native_clone import NativeClone
from vibearr_runner.recovery import ReconciliationRequired


class NativeCloneTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.journal = Journal(self.root / "journal")
        self.journal.__enter__()
        self.addCleanup(self.journal.__exit__)
        self.host = SimpleNamespace(
            run_id=str(uuid4()), native={}, state={}, journal=self.journal, save=Mock()
        )
        manager = SimpleNamespace(directory=self.root / "workspaces")
        self.clone = NativeClone(
            self.host,
            manager,
            Mock(),
            {"repository_url": "https://github.com/owner/repo.git"},
        )
        self.clone.checkout.mkdir(parents=True)
        (self.clone.checkout / "partial").write_text("incomplete")

    def register(self, state):
        self.host.native["clone"] = {"binding": self.clone.binding, "state": state}

    def test_unregistered_partial_checkout_is_preserved(self):
        with self.assertRaises(ReconciliationRequired):
            self.clone.recover()
        self.assertTrue((self.clone.checkout / "partial").exists())

    def test_unconfirmed_process_termination_preserves_partial_checkout(self):
        self.register("dispatched")
        with patch(
            "vibearr_runner.native_clone.ProcessTracker.recover",
            side_effect=ReconciliationRequired("process unknown"),
        ):
            with self.assertRaises(ReconciliationRequired):
                self.clone.recover()
        self.assertTrue((self.clone.checkout / "partial").exists())

    def test_confirmed_interrupted_clone_removes_only_its_own_partial_checkout(self):
        self.register("dispatched")
        unrelated = self.clone.directory.parent / str(uuid4()) / "checkout"
        unrelated.mkdir(parents=True)
        self.assertFalse(self.clone.recover())
        self.assertFalse(self.clone.checkout.exists())
        self.assertTrue(unrelated.exists())

    def test_completed_clone_is_reused_after_confirmed_process_recovery(self):
        self.register("completed")
        self.assertTrue(self.clone.recover())
        self.assertTrue((self.clone.checkout / "partial").exists())

    def test_changed_clone_binding_does_not_adopt_or_remove_checkout(self):
        self.register("dispatched")
        self.host.native["clone"]["binding"] = {"arguments": ["different"]}
        with self.assertRaises(ReconciliationRequired):
            self.clone.recover()
        self.assertTrue((self.clone.checkout / "partial").exists())
