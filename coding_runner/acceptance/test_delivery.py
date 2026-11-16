import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock
from uuid import uuid4

from coding_runner.client import AmbiguousRequest
from coding_runner.delivery import (
    DeliveryError,
    DeliveryStage,
    DeliveryUnknown,
    GitHubDeliveryClient,
    SupervisorGit,
    description,
    pull_identity,
)
from coding_runner.journal import Journal
from coding_runner.workspaces import WorkspaceManager, git


class FakeGitHub:
    def __init__(self):
        self.pull = None
        self.creates = 0
        self.lose_create = False

    def find(self, repository, head, base, commit):
        if self.pull:
            self.pull["commit_sha"] = commit
        return self.pull

    def create(self, repository, head, base, commit, title, body):
        self.creates += 1
        self.pull = {
            "number": 1,
            "url": f"https://github.com/{repository}/pull/1",
            "repository": repository,
            "head": head,
            "base": base,
            "commit_sha": commit,
        }
        if self.lose_create:
            raise DeliveryUnknown("Creation response lost.")
        return self.pull


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.remote = self.root / "remote.git"
        git(["init", "--bare", "--initial-branch=main", str(self.remote)])
        seed = self.root / "seed"
        git(["clone", str(self.remote), str(seed)], local=True)
        (seed / "README.md").write_text("Before\n")
        git(["add", "README.md"], seed)
        git(
            [
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.com",
                "commit",
                "-m",
                "initial",
            ],
            seed,
        )
        git(["push", "origin", "main"], seed, local=True)
        self.run_id = str(uuid4())
        manager = WorkspaceManager(
            self.root / "workspaces", trusted_local_root=self.root
        )
        self.workspace = manager.prepare(
            self.run_id,
            {
                "repository_url": str(self.remote),
                "base_branch": "main",
                "branch_name": "nq/task",
            },
        )
        # Production identity stays GitHub-bound while the injected transport uses a local bare repository.
        self.local_workspace = self.workspace
        from dataclasses import replace

        self.workspace = replace(
            self.workspace, repository_url="https://github.com/example/repo"
        )
        metadata = self.workspace.directory / "workspace.json"
        saved = json.loads(metadata.read_text())
        saved["repository_url"] = self.workspace.repository_url
        metadata.write_text(json.dumps(saved))
        (self.workspace.checkout / "README.md").write_text("After\n")
        self.journal = Journal(self.root / "journal")
        self.journal.__enter__()
        self.addCleanup(self.journal.__exit__, None, None, None)
        self.state = self.journal.load()
        self.github = FakeGitHub()
        self.client = Mock()
        self.client.reconcile.return_value = {"run": {"state": "active"}}
        self.plan = {
            "problem": "Preserve retries",
            "approach": "Store delivery observations",
            "original_issue": "https://github.com/example/repo/issues/4",
        }
        self.repo_git = SupervisorGit(trusted_local=True)
        local_remote = self.remote
        original_remote = self.repo_git.remote

        def remote(workspace, arguments):
            return original_remote(
                workspace,
                [
                    str(local_remote) if entry == workspace.repository_url else entry
                    for entry in arguments
                ],
            )

        self.repo_git.remote = remote
        from coding_runner.implementation import workspace_evidence

        fingerprint = workspace_evidence(self.workspace)
        self.evidence = {
            "schema_version": 1,
            "plan_id": str(uuid4()),
            "plan_digest": "a" * 64,
            "context_id": str(uuid4()),
            "workspace": fingerprint,
            "validation": [
                {
                    "phase": "validation",
                    "action": {},
                    "execution_id": str(uuid4()),
                    "exit_code": 0,
                    "interrupted": False,
                    "before": fingerprint,
                    "after": fingerprint,
                    "output_digest": "a" * 64,
                    "output_bytes": 1,
                }
            ],
            "amendments": [],
        }
        self.stage = DeliveryStage(
            self.journal,
            self.client,
            self.github,
            repository_git=self.repo_git,
            gate=lambda: True,
        )

    def step(self):
        return self.stage.step(
            self.state, self.run_id, self.workspace, self.evidence, self.plan, "claim"
        )

    def test_delivery_commits_pushes_records_then_finishes(self):
        self.assertEqual(self.step()["status"], "finished")
        head = git(["rev-parse", "HEAD"], self.workspace.checkout)
        self.assertEqual(self.repo_git.observed_remote(self.workspace), head)
        self.assertEqual(self.github.creates, 1)
        calls = self.client.request.call_args_list
        self.assertEqual(calls[0].args[2]["kind"], "verified_delivery")
        self.assertTrue(calls[1].args[1].endswith("/finish/"))

    def test_repeat_finished_delivery_does_not_create_or_commit_again(self):
        self.step()
        head = git(["rev-parse", "HEAD"], self.workspace.checkout)
        self.step()
        self.assertEqual(git(["rev-parse", "HEAD"], self.workspace.checkout), head)
        self.assertEqual(self.github.creates, 1)

    def test_existing_pr_is_preserved(self):
        self.github.pull = {
            "number": 7,
            "url": "https://github.com/example/repo/pull/7",
            "repository": "example/repo",
            "head": "nq/task",
            "base": "main",
            "commit_sha": "",
        }
        self.assertEqual(self.step()["pull_request"]["number"], 7)
        self.assertEqual(self.github.creates, 0)

    def test_push_failure_does_not_create_pr(self):
        self.repo_git.push = Mock(side_effect=DeliveryError("Push failed."))
        self.assertEqual(self.step()["status"], "failed")
        self.assertEqual(self.github.creates, 0)
        self.assertEqual(self.state["delivery"]["phase"], "push_pending")

    def test_lost_create_response_reconciles_existing_pr(self):
        self.github.lose_create = True
        self.assertEqual(self.step()["status"], "reconciliation")
        self.assertEqual(self.step()["status"], "finished")
        self.assertEqual(self.github.creates, 1)

    def test_unknown_create_without_visible_pr_never_recreates(self):
        self.github.lose_create = True
        self.step()
        self.github.pull = None
        self.assertEqual(self.step()["status"], "reconciliation")
        self.assertEqual(self.github.creates, 1)

    def test_finish_timeout_retries_only_reporting(self):
        def request(method, path, payload):
            if path.endswith("/finish/"):
                raise AmbiguousRequest("Lost finish response")

        self.client.request.side_effect = request
        self.assertEqual(self.step()["status"], "reporting_pending")
        self.stage.verify = Mock(
            side_effect=AssertionError("Validation should not rerun")
        )
        self.repo_git.push = Mock(side_effect=AssertionError("Push should not rerun"))
        self.client.reconcile.return_value = {
            "run": {
                "state": "completed",
                "result_status": "succeeded",
                "result_url": self.github.pull["url"],
            }
        }
        self.assertEqual(self.step()["status"], "finished")
        self.assertEqual(self.github.creates, 1)

    def test_validation_change_prevents_commit(self):
        (self.workspace.checkout / "README.md").write_text("Unvalidated\n")
        before = git(["rev-parse", "HEAD"], self.workspace.checkout)
        result = self.step()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(git(["rev-parse", "HEAD"], self.workspace.checkout), before)

    def test_gate_closure_prevents_delivery(self):
        self.stage.gate = lambda: False
        self.assertEqual(self.step()["status"], "failed")
        self.assertEqual(self.github.creates, 0)

    def test_commit_is_reconciled_after_journal_response_loss(self):
        original_save = self.stage.save

        def save(state, delivery):
            if delivery.get("commit_sha"):
                raise OSError("Crash before commit observation persisted")
            original_save(state, delivery)

        self.stage.save = save
        with self.assertRaises(OSError):
            self.step()
        self.state = self.journal.load()
        self.stage.save = original_save
        head = git(["rev-parse", "HEAD"], self.workspace.checkout)
        self.assertEqual(self.step()["status"], "finished")
        self.assertEqual(git(["rev-parse", "HEAD"], self.workspace.checkout), head)

    def test_pr_description_includes_problem_validation_and_original_issue(self):
        body = description(self.plan, self.evidence)
        self.assertIn(self.plan["problem"], body)
        self.assertIn(self.plan["original_issue"], body)
        self.assertIn("exit 0", body)


class PullIdentityTests(unittest.TestCase):
    def setUp(self):
        self.raw = {
            "number": 1,
            "html_url": "https://github.com/example/repo/pull/1",
            "state": "open",
            "head": {
                "repo": {"full_name": "example/repo"},
                "ref": "nq/task",
                "sha": "a" * 40,
            },
            "base": {"repo": {"full_name": "example/repo"}, "ref": "main"},
        }

    def test_actual_repository_head_base_and_sha_are_required(self):
        self.assertEqual(
            pull_identity(self.raw, "example/repo", "nq/task", "main", "a" * 40)[
                "number"
            ],
            1,
        )

    def test_wrong_repo_or_head_or_empty_url_are_rejected(self):
        for key, value in (
            ("html_url", ""),
            ("html_url", "https://github.com/other/repo/pull/1"),
            ("state", "closed"),
        ):
            raw = {**self.raw, key: value}
            with self.subTest(key=key, value=value), self.assertRaises(DeliveryError):
                pull_identity(raw, "example/repo", "nq/task", "main", "a" * 40)
        raw = copy.deepcopy(self.raw)
        raw["head"]["ref"] = "another"
        with self.assertRaises(DeliveryError):
            pull_identity(raw, "example/repo", "nq/task", "main", "a" * 40)

    def test_github_client_refuses_wrong_returned_repository(self):
        client = GitHubDeliveryClient("test-token")
        client.request = Mock(
            return_value=[
                {**self.raw, "html_url": "https://github.com/evil/repo/pull/1"}
            ]
        )
        with self.assertRaises(DeliveryError):
            client.find("example/repo", "nq/task", "main", "a" * 40)

    def test_github_client_disables_redirects_and_environment_proxies(self):
        client = GitHubDeliveryClient("test-token")
        from coding_runner.client import NoRedirects

        self.assertTrue(
            any(isinstance(handler, NoRedirects) for handler in client.opener.handlers)
        )


if __name__ == "__main__":
    unittest.main()
