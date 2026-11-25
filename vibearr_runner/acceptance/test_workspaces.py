import hashlib
import json
import os
import unittest
from dataclasses import asdict, replace
from unittest.mock import patch
from uuid import uuid4


from vibearr_runner.test_workspaces import WorkspaceFixture
from vibearr_runner.workspaces import WorkspaceError, content_digest, git


class WorkspaceTests(WorkspaceFixture, unittest.TestCase):
    def setUp(self):
        self.create_workspace()

    def test_blank_base_resolves_remote_default_and_records_commit(self):
        self.assertEqual(self.workspace.resolved_base_branch, "trunk")
        self.assertEqual(
            self.workspace.base_revision, git(["rev-parse", "HEAD"], self.repository)
        )
        self.assertEqual(
            git(["branch", "--show-current"], self.workspace.checkout), "task/example"
        )

    def test_top_level_claim_settings_select_repository_and_base(self):
        git(["checkout", "-b", "release"], self.repository)
        (self.repository / "release").write_text("release branch")
        git(["add", "release"], self.repository)
        git(
            [
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.com",
                "commit",
                "-m",
                "release",
            ],
            self.repository,
        )
        item = {
            **self.item,
            "base_branch": "release",
            "project": {"repository_url": "https://evil.invalid/wrong"},
        }
        workspace = self.manager.prepare(str(uuid4()), item)
        self.assertEqual(workspace.resolved_base_branch, "release")
        self.assertTrue((workspace.checkout / "release").exists())

    def test_restart_preserves_task_changes(self):
        (self.workspace.checkout / "README.md").write_text("work in progress")
        restored = self.manager.prepare(self.workspace.workspace_id, self.item)
        self.assertEqual(
            (restored.checkout / "README.md").read_text(), "work in progress"
        )

    def test_task_branch_cannot_equal_explicit_base(self):
        item = {**self.item, "base_branch": "trunk", "branch_name": "trunk"}
        with self.assertRaisesRegex(WorkspaceError, "must differ"):
            self.manager.prepare(str(uuid4()), item)

    def test_task_branch_cannot_equal_resolved_remote_default(self):
        item = {**self.item, "branch_name": "trunk"}
        with self.assertRaisesRegex(WorkspaceError, "must differ"):
            self.manager.prepare(str(uuid4()), item)

    def test_restart_rejects_previously_saved_base_as_task_branch(self):
        git(["checkout", "trunk", "--"], self.workspace.checkout)
        saved = replace(self.workspace, branch_name="trunk")
        metadata = {**asdict(saved), "directory": str(saved.directory)}
        (saved.directory / "workspace.json").write_text(json.dumps(metadata))
        item = {**self.item, "branch_name": "trunk"}
        with self.assertRaisesRegex(WorkspaceError, "must differ"):
            self.manager.prepare(saved.workspace_id, item)

    def test_retry_reuses_remote_task_branch_without_reset(self):
        git(["checkout", "-b", "task/example"], self.repository)
        (self.repository / "previous-work").write_text("preserve remote work")
        git(["add", "previous-work"], self.repository)
        git(
            [
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.com",
                "commit",
                "-m",
                "previous work",
            ],
            self.repository,
        )
        git(["checkout", "trunk", "--"], self.repository)
        workspace = self.manager.prepare(str(uuid4()), self.item)
        self.assertTrue((workspace.checkout / "previous-work").exists())

    def test_changed_claim_settings_cannot_reuse_checkout(self):
        with self.assertRaises(WorkspaceError):
            self.manager.prepare(
                self.workspace.workspace_id, {**self.item, "branch_name": "other"}
            )

    def test_option_injection_is_rejected_before_git(self):
        marker = self.root / "injected"
        values = [
            "--upload-pack=touch " + str(marker),
            "-b",
            "trunk\n--help",
            "bad..branch",
            "x@{1}",
        ]
        for branch in values:
            with self.subTest(branch=branch), self.assertRaises(WorkspaceError):
                self.manager.prepare(str(uuid4()), {**self.item, "branch_name": branch})
        self.assertFalse(marker.exists())

    def test_repository_protocols_credentials_and_option_values_are_rejected(self):
        values = [
            "--upload-pack=evil",
            "ext::touch /tmp/evil",
            "ssh://example.com/repo",
            "https://u:p@example.com/repo",
        ]
        for value in values:
            with self.subTest(value=value), self.assertRaises(WorkspaceError):
                self.manager.prepare(
                    str(uuid4()), {**self.item, "repository_url": value}
                )

    def test_global_git_hooks_and_inherited_git_settings_cannot_execute(self):
        hooks = self.root / "hooks"
        hooks.mkdir()
        marker = self.root / "hook-executed"
        script = hooks / "post-checkout"
        script.write_text("#!/bin/sh\ntouch " + str(marker) + "\n")
        script.chmod(0o700)
        global_config = self.root / "global.gitconfig"
        global_config.write_text("[core]\n\thooksPath = " + str(hooks) + "\n")
        environment = {
            "GIT_CONFIG_GLOBAL": str(global_config),
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "core.hooksPath",
            "GIT_CONFIG_VALUE_0": str(hooks),
        }
        with patch.dict(os.environ, environment):
            self.manager.prepare(str(uuid4()), self.item)
        self.assertFalse(marker.exists())

    def test_repository_source_digest_is_recomputed_from_current_file(self):
        reference = "README.md"
        digest = self.sources.current_digest("repository", reference, self.workspace)
        self.assertEqual(
            digest,
            hashlib.sha256(
                (self.workspace.checkout / reference).read_bytes()
            ).hexdigest(),
        )
        (self.workspace.checkout / reference).write_text("changed instructions")
        self.assertNotEqual(
            digest, self.sources.current_digest("repository", reference, self.workspace)
        )

    def test_repository_provenance_cannot_follow_external_symlink(self):
        (self.workspace.checkout / "escape").symlink_to(
            self.root / "supervisor-sources"
        )
        with self.assertRaises(WorkspaceError):
            self.sources.current_digest("repository", "escape/file", self.workspace)

    def test_content_digest_covers_untracked_files_symlinks_and_modes(self):
        previous = content_digest(self.workspace.checkout)
        path = self.workspace.checkout / "new-file"
        path.write_text("new")
        self.assertNotEqual(previous, content_digest(self.workspace.checkout))
        previous = content_digest(self.workspace.checkout)
        path.chmod(0o700)
        self.assertNotEqual(previous, content_digest(self.workspace.checkout))
        previous = content_digest(self.workspace.checkout)
        (self.workspace.checkout / "link").symlink_to("new-file")
        self.assertNotEqual(previous, content_digest(self.workspace.checkout))

    def test_hard_link_and_special_file_cannot_be_approved(self):
        os.link(
            self.workspace.checkout / "README.md", self.workspace.checkout / "hard-link"
        )
        with self.assertRaises(WorkspaceError):
            content_digest(self.workspace.checkout)

    def test_fifo_is_rejected_without_blocking(self):
        os.mkfifo(self.workspace.checkout / "fifo")
        with self.assertRaises(WorkspaceError):
            content_digest(self.workspace.checkout)

    def test_workspace_lock_prevents_overlapping_operations(self):
        with (
            self.workspace.locked(),
            self.assertRaises(WorkspaceError),
            self.workspace.locked(),
        ):
            pass


if __name__ == "__main__":
    unittest.main()
