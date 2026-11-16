from pathlib import Path
import importlib.util
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from vibearr_runner.journal import Journal
from vibearr_runner.native_workspace import (
    NoChange,
    commit_candidate,
    import_files,
    recover_import,
)
from vibearr_runner.workspaces import WorkspaceError, WorkspaceManager, git

SDK_AVAILABLE = importlib.util.find_spec("vibearr_executor") is not None
if SDK_AVAILABLE:
    from vibearr_executor.artifacts import pack, result_manifest, snapshot


@unittest.skipUnless(
    SDK_AVAILABLE, "Install the native-runtime extra for native workspace tests"
)
class NativeWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        git(["init", "-b", "main", str(self.source)], local=True)
        (self.source / "old.py").write_text("answer = 1\n")
        git(["add", "."], self.source)
        git(
            [
                "-c",
                "user.name=test",
                "-c",
                "user.email=test@example.test",
                "commit",
                "-m",
                "base",
            ],
            self.source,
        )
        self.run_id = str(uuid4())
        self.manager = WorkspaceManager(
            self.root / "workspaces", trusted_local_root=self.root
        )
        self.workspace = self.manager.prepare(
            self.run_id,
            {
                "repository_url": str(self.source),
                "base_branch": "main",
                "branch_name": "task/example",
            },
        )
        self.journal = Journal(self.root / "journal")
        self.journal.__enter__()
        self.state = self.journal.load()
        self.state["native"] = {}
        self.original = snapshot(self.workspace.checkout)

    def tearDown(self):
        self.journal.__exit__()
        self.temporary.cleanup()

    def test_verified_import_adds_deletes_and_keeps_trusted_git(self):
        result = pack({"new.sh": (0o755, b"#!/bin/sh\nexit 0\n")})
        import_files(
            self.workspace,
            self.original,
            result,
            result_manifest(self.original, result),
            str(uuid4()),
            self.journal,
            self.state,
        )
        self.assertFalse((self.workspace.checkout / "old.py").exists())
        self.assertEqual(
            (self.workspace.checkout / "new.sh").stat().st_mode & 0o777, 0o755
        )
        self.assertEqual(
            git(["rev-parse", "HEAD"], self.workspace.checkout),
            self.workspace.base_revision,
        )

    def test_import_recovers_missing_checkout_between_directory_renames(self):
        result = pack({"new.py": (0o644, b"answer = 2\n")})
        operation = str(uuid4())
        original_rename = __import__("os").rename

        def fail_second(source, target):
            if Path(target) == self.workspace.checkout:
                raise OSError("simulated controller restart")
            original_rename(source, target)

        with patch(
            "vibearr_runner.native_workspace.os.rename", side_effect=fail_second
        ):
            with self.assertRaises(OSError):
                import_files(
                    self.workspace,
                    self.original,
                    result,
                    result_manifest(self.original, result),
                    operation,
                    self.journal,
                    self.state,
                )
        self.assertFalse(self.workspace.checkout.exists())
        recover_import(self.workspace.directory, self.journal, self.state)
        self.assertEqual(snapshot(self.workspace.checkout), result)
        self.assertNotIn("import", self.state["native"])

    def test_commit_reconciles_a_lost_response_without_another_commit(self):
        (self.workspace.checkout / "old.py").write_text("answer = 2\n")
        base_tree = git(["rev-parse", "HEAD^{tree}"], self.workspace.checkout)
        operation = str(uuid4())
        first = commit_candidate(
            self.workspace,
            base_tree=base_tree,
            operation_id=operation,
            title="Change answer",
            journal=self.journal,
            state=self.state,
        )
        second = commit_candidate(
            self.workspace,
            base_tree=base_tree,
            operation_id=operation,
            title="Change answer",
            journal=self.journal,
            state=self.state,
        )
        self.assertEqual(first, second)
        self.assertEqual(
            git(["rev-list", "--count", "HEAD"], self.workspace.checkout), "2"
        )

    def test_empty_tree_never_creates_a_commit(self):
        base_tree = git(["rev-parse", "HEAD^{tree}"], self.workspace.checkout)
        with self.assertRaises(NoChange):
            commit_candidate(
                self.workspace,
                base_tree=base_tree,
                operation_id=str(uuid4()),
                title="No change",
                journal=self.journal,
                state=self.state,
            )
        self.assertEqual(
            git(["rev-list", "--count", "HEAD"], self.workspace.checkout), "1"
        )

    def test_new_native_workspace_refuses_unrelated_remote_branch(self):
        git(["branch", "task/existing"], self.source)
        manager = WorkspaceManager(
            self.root / "strict", trusted_local_root=self.root, require_new_branch=True
        )
        with self.assertRaisesRegex(WorkspaceError, "existing remote task branch"):
            manager.prepare(
                str(uuid4()),
                {
                    "repository_url": str(self.source),
                    "base_branch": "main",
                    "branch_name": "task/existing",
                },
            )

    def test_inventory_mismatch_preserves_original_checkout(self):
        result = pack({"new.py": (0o644, b"answer = 2\n")})
        with self.assertRaises(WorkspaceError):
            import_files(
                self.workspace,
                self.original,
                result,
                {},
                str(uuid4()),
                self.journal,
                self.state,
            )
        self.assertEqual(snapshot(self.workspace.checkout), self.original)
