import base64
from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from vibearr_runner.delivery import DeliveryError
from vibearr_runner.merge_github import (
    MergeGitHub,
    MergeRepair,
    MergeWait,
    merged_observation,
)
from vibearr_runner.native_reads import read


def profile():
    return {
        "checks": [
            {
                "name": "Tests",
                "app_id": 15368,
                "workflow_id": 12,
                "workflow_path": ".github/workflows/test.yml",
            }
        ]
    }


def workspace():
    return {
        "repository_id": 17,
        "repository": "owner/repo",
        "branch": "task/1",
        "base_branch": "main",
        "base_revision": "a" * 40,
        "commit_sha": "b" * 40,
        "tree_sha": "c" * 40,
    }


def pull_raw():
    value = workspace()
    return {
        "number": 9,
        "html_url": "https://github.com/owner/repo/pull/9",
        "state": "open",
        "merged": False,
        "draft": False,
        "mergeable": True,
        "mergeable_state": "clean",
        "head": {
            "repo": {"id": 17, "full_name": "owner/repo"},
            "ref": value["branch"],
            "sha": value["commit_sha"],
        },
        "base": {
            "repo": {"id": 17, "full_name": "owner/repo"},
            "ref": "main",
            "sha": value["base_revision"],
        },
    }


class Provider(MergeGitHub):
    def __init__(self):
        self.workflow = {
            "id": 20,
            "workflow_id": 12,
            "path": ".github/workflows/test.yml",
            "event": "pull_request",
            "head_sha": "b" * 40,
            "head_repository": {"id": 17},
            "check_suite_id": 10,
            "run_attempt": 1,
            "status": "completed",
            "conclusion": "success",
            "pull_requests": [
                {
                    "number": 9,
                    "base": {"ref": "main", "sha": "a" * 40, "repo": {"id": 17}},
                    "head": {"ref": "task/1", "sha": "b" * 40, "repo": {"id": 17}},
                }
            ],
        }
        self.check = {
            "id": 30,
            "name": "Tests",
            "head_sha": "b" * 40,
            "app": {"id": 15368},
            "check_suite": {"id": 10},
            "status": "completed",
            "conclusion": "success",
            "started_at": "2026-11-16T01:00:00Z",
            "completed_at": "2026-11-16T01:01:00Z",
            "html_url": "https://github.com/owner/repo/runs/30",
        }
        self.job = {
            "id": 30,
            "name": "Tests",
            "head_sha": "b" * 40,
            "status": "completed",
            "conclusion": "success",
        }
        self.checks_extra = []
        self.newer = None

    def request(self, method, path, payload=None):
        if method != "GET" or payload is not None:
            raise AssertionError(
                "Publication observations must use read-only requests."
            )
        if "/check-runs?" in path:
            checks = [self.check, *self.checks_extra]
            return {"total_count": len(checks), "check_runs": checks}
        if "/actions/workflows/12/runs?" in path:
            runs = [self.workflow] + ([self.newer] if self.newer else [])
            return {"total_count": len(runs), "workflow_runs": runs}
        if "/jobs?" in path:
            return {"total_count": 1, "jobs": [self.job]}
        raise AssertionError(path)


class PublicationProviderTests(unittest.TestCase):
    def setUp(self):
        self.provider = Provider()

    def test_current_exact_head_checks_with_trusted_producers_pass(self):
        self.assertEqual(len(self.provider.checks(workspace(), 9, profile())), 1)

    def test_pending_ci_polls_do_not_accumulate_transport_failures(self):
        host = SimpleNamespace(native={}, save=lambda: None)
        request = self.provider.request
        self.provider.check["status"] = "in_progress"

        def observed_request(method, path, payload=None):
            return read(host, "github:" + path, lambda: request(method, path, payload))

        with patch.object(self.provider, "request", side_effect=observed_request):
            for _ in range(5):
                with self.assertRaises(MergeWait):
                    self.provider.checks(workspace(), 9, profile())
        self.assertEqual(host.native["transport_reads"], {})

    def test_check_from_wrong_producer_is_rejected(self):
        self.provider.check["app"]["id"] = 999
        with self.assertRaises(DeliveryError):
            self.provider.checks(workspace(), 9, profile())

    def test_duplicate_forged_check_cannot_hide_behind_success(self):
        self.provider.checks_extra = [deepcopy(self.provider.check)]
        self.provider.checks_extra[0]["app"]["id"] = 999
        with self.assertRaises(DeliveryError):
            self.provider.checks(workspace(), 9, profile())

    def test_old_cancelled_suite_never_replaces_latest_success(self):
        old = deepcopy(self.provider.check)
        old.update(id=29, conclusion="cancelled", check_suite={"id": 9})
        self.provider.checks_extra = [old]
        result = self.provider.checks(workspace(), 9, profile())
        self.assertEqual([value["check_id"] for value in result], [30])
        self.assertEqual(
            result[0]["suite_id"], self.provider.workflow["check_suite_id"]
        )

    def test_old_cancelled_suite_does_not_hide_latest_pending_check(self):
        old = deepcopy(self.provider.check)
        old.update(id=29, conclusion="cancelled", check_suite={"id": 9})
        self.provider.checks_extra = [old]
        self.provider.check["status"] = "in_progress"
        self.provider.check["conclusion"] = None
        with self.assertRaises(MergeWait):
            self.provider.checks(workspace(), 9, profile())

    def test_duplicate_current_suite_is_rejected_with_same_app(self):
        self.provider.checks_extra = [deepcopy(self.provider.check)]
        self.provider.checks_extra[0]["id"] = 31
        with self.assertRaises(DeliveryError):
            self.provider.checks(workspace(), 9, profile())

    def test_pending_check_waits(self):
        self.provider.check["status"] = "in_progress"
        with self.assertRaises(MergeWait):
            self.provider.checks(workspace(), 9, profile())

    def test_skipped_check_requires_repair(self):
        self.provider.check["conclusion"] = "skipped"
        with self.assertRaises(MergeRepair):
            self.provider.checks(workspace(), 9, profile())

    def test_new_pending_workflow_supersedes_old_success(self):
        self.provider.newer = {
            **self.provider.workflow,
            "id": 21,
            "check_suite_id": 11,
            "status": "queued",
            "conclusion": None,
        }
        with self.assertRaises(MergeWait):
            self.provider.checks(workspace(), 9, profile())

    def test_latest_attempt_must_own_exact_check(self):
        self.provider.job["id"] = 31
        with self.assertRaises(DeliveryError):
            self.provider.checks(workspace(), 9, profile())

    def test_old_base_workflow_cannot_authorize_current_base(self):
        self.provider.workflow["pull_requests"][0]["base"]["sha"] = "e" * 40
        with self.assertRaises(MergeRepair):
            self.provider.checks(workspace(), 9, profile())

    def test_closed_unmerged_pr_is_not_merge_evidence(self):
        value = pull_raw()
        value["state"] = "closed"
        self.assertIsNone(merged_observation(value, workspace(), 9))

    def test_merged_different_head_is_rejected(self):
        value = pull_raw()
        value["merged"] = True
        value["head"]["sha"] = "e" * 40
        with self.assertRaises(DeliveryError):
            merged_observation(value, workspace(), 9)


class ProviderContentTests(unittest.TestCase):
    def setUp(self):
        self.provider = MergeGitHub("publication-test-token", read_only=True)
        self.response = {
            "type": "file",
            "encoding": "base64",
            "path": ".github/workflows/test.yml",
            "content": base64.b64encode(b"reviewed workflow").decode(),
        }
        observed = patch.object(self.provider, "request", return_value=self.response)
        observed.start()
        self.addCleanup(observed.stop)

    def test_exact_reviewed_workflow_file_can_be_read(self):
        self.assertEqual(
            self.provider.content("owner/repo", ".github/workflows/test.yml", "a" * 40),
            b"reviewed workflow",
        )

    def test_another_file_cannot_substitute_for_the_requested_workflow(self):
        self.response["path"] = "candidate.yml"
        with self.assertRaises(DeliveryError):
            self.provider.content("owner/repo", ".github/workflows/test.yml", "a" * 40)

    def test_workflow_file_read_is_bounded(self):
        self.response["content"] = base64.b64encode(b"x" * 262145).decode()
        with self.assertRaises(DeliveryError):
            self.provider.content("owner/repo", ".github/workflows/test.yml", "a" * 40)
