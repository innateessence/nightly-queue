from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from vibearr_runner.delivery import DeliveryError, DeliveryRejected, DeliveryUnknown
from vibearr_runner.merge_contract import merge_profile, merge_receipt, review_report
from vibearr_runner.merge_github import (
    MergeGitHub,
    MergeRepair,
    MergeWait,
    merged_observation,
)
from vibearr_runner.native_contract import NativeContractError, digest
from vibearr_runner.native_merge import NativeMerge
from vibearr_runner.merge_review import publish_review
from vibearr_runner.merge_setup import apply_setup
from vibearr_runner.merge_acceptance import merge_waypoint
from vibearr_runner.policy import ExecutionPolicy
from vibearr_runner.recovery import ReconciliationRequired


def profile():
    return {
        "schema_version": 1,
        "repository_id": 17,
        "actor_id": 25,
        "ci_timeout_seconds": 1800,
        "delete_branch": False,
        "checks": [
            {
                "name": "Tests",
                "app_id": 15368,
                "workflow_id": 12,
                "workflow_path": ".github/workflows/test.yml",
                "workflow_digest": hashlib.sha256(b"trusted").hexdigest(),
            }
        ],
        "reviews": [
            {
                "role": role,
                "context": "ai-review/" + role,
                "creator_id": 25,
                "report_repository": "owner/reports",
                "report_repository_id": 18,
                "report_prefix": "reports/",
            }
            for role in ("independent", "root")
        ],
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


def report(role, run_id="run", context_digest="d" * 64):
    value = workspace()
    return {
        "schema_version": 1,
        "role": role,
        "run_id": run_id,
        "context_digest": context_digest,
        "repository_id": 17,
        "pull_number": 9,
        "head_sha": value["commit_sha"],
        "base_sha": value["base_revision"],
        "tree_sha": value["tree_sha"],
        "session_id": "session-" + role,
        "reviewer": "trusted AI coordinator",
        "findings": [],
        "verdict": "no_p2_or_higher",
    }


def receipt(run_id="run"):
    return {
        "policy": {
            "actor_id": 25,
            "profile_digest": digest(profile()),
            "rulesets": [{"id": 5, "digest": "d" * 64}],
        },
        "checks": [
            {
                "name": "Tests",
                "producer_id": 15368,
                "head_sha": workspace()["commit_sha"],
                "conclusion": "success",
                "url": "https://github.com/owner/repo/runs/30",
                "check_id": 30,
                "workflow_id": 12,
                "run_id": 20,
                "run_attempt": 1,
                "suite_id": 10,
            }
        ],
        "reviews": [
            {
                "role": role,
                "status_id": index + 40,
                "creator_id": 25,
                "context": "ai-review/" + role,
                "report_url": "https://github.com/owner/reports/blob/"
                + "e" * 40
                + "/reports/"
                + role
                + ".json",
                "report_digest": "f" * 64,
                "report": report(role, run_id),
            }
            for index, role in enumerate(("independent", "root"))
        ],
    }


class Provider(MergeGitHub):
    def __init__(self):
        self.raw = pull_raw()
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
        self.rules = [
            {
                "type": "required_status_checks",
                "ruleset_id": 5,
                "parameters": {
                    "strict_required_status_checks_policy": True,
                    "required_status_checks": [
                        {"context": "Tests", "integration_id": 15368},
                        {"context": "ai-review/independent", "integration_id": None},
                        {"context": "ai-review/root", "integration_id": None},
                    ],
                },
            },
            {
                "type": "pull_request",
                "ruleset_id": 5,
                "parameters": {"required_approving_review_count": 0},
            },
        ]
        self.bypass = []
        self.requests = []
        self.checks_extra = []
        self.newer = None

    def repository(self, **kwargs):
        return {"repository_id": 17, "repository": "owner/repo"}, {}

    def content(self, *args):
        return b"trusted"

    def request(self, method, path, payload=None):
        self.requests.append((method, path, payload))
        if path == "/user":
            return {"id": 25}
        if path == "/repos/owner/repo":
            return {"allow_squash_merge": True}
        if "/rules/branches/" in path:
            return self.rules
        if "/rulesets/" in path:
            return {"id": 5, "enforcement": "active", "bypass_actors": self.bypass}
        if path.endswith("/protection"):
            raise DeliveryRejected(404)
        if "/actions/workflows/12?" in path or path.endswith("/actions/workflows/12"):
            return {"id": 12, "path": ".github/workflows/test.yml", "state": "active"}
        if "/check-runs?" in path:
            checks = [self.check, *self.checks_extra]
            return {"total_count": len(checks), "check_runs": checks}
        if "/actions/workflows/12/runs?" in path:
            runs = [self.workflow] + ([self.newer] if self.newer else [])
            return {"total_count": len(runs), "workflow_runs": runs}
        if "/jobs?" in path:
            return {"total_count": 1, "jobs": [self.job]}
        if path.endswith("/check-runs/30"):
            return self.check
        if path.endswith("/actions/runs/20/attempts/1"):
            return self.workflow
        raise AssertionError(path)


class MergeProfileTests(unittest.TestCase):
    def test_reviews_are_disabled_by_default_in_merge_profile(self):
        value = profile()
        value.pop("reviews")
        self.assertEqual(merge_profile(value)["reviews"], [])

    def test_ci_only_profile_requires_no_review_statuses(self):
        value = profile()
        value["reviews"] = []
        provider = Provider()
        provider.rules[0]["parameters"]["required_status_checks"] = [
            {"context": "Tests", "integration_id": 15368}
        ]
        self.assertEqual(
            provider.policy(workspace(), value)["profile_digest"], digest(value)
        )
        self.assertEqual(
            provider.reviews(
                workspace(), 9, value, run_id="run", context_digest="d" * 64
            ),
            [],
        )

    def test_ci_only_receipt_does_not_fabricate_reviews(self):
        value, observed = profile(), receipt()
        value["reviews"] = []
        observed["reviews"] = []
        observed["policy"]["profile_digest"] = digest(value)
        self.assertEqual(merge_receipt(observed, value, workspace())["reviews"], [])

    def test_empty_check_profile_is_rejected(self):
        value = profile()
        value["checks"] = []
        with self.assertRaises(NativeContractError):
            merge_profile(value)

    def test_two_review_roles_may_share_provider_actor(self):
        self.assertEqual(merge_profile(profile()), profile())

    def test_missing_required_receipt_is_rejected(self):
        value = receipt()
        value["checks"] = []
        with self.assertRaises(NativeContractError):
            merge_receipt(value, profile(), workspace())

    def test_old_head_review_cannot_authorize_current_candidate(self):
        value = report("root")
        value["head_sha"] = "e" * 40
        with self.assertRaises(NativeContractError):
            review_report(
                value,
                role="root",
                run_id="run",
                context_digest="d" * 64,
                workspace=workspace(),
                pull_number=9,
            )

    def test_unresolved_p2_review_is_rejected(self):
        value = report("root")
        value["findings"] = [{"priority": "P2", "description": "Unresolved."}]
        with self.assertRaises(NativeContractError):
            review_report(
                value,
                role="root",
                run_id="run",
                context_digest="d" * 64,
                workspace=workspace(),
                pull_number=9,
            )


class MergeProviderTests(unittest.TestCase):
    def setUp(self):
        self.provider = Provider()

    def test_nonempty_current_rules_and_trusted_producers_pass(self):
        self.assertEqual(self.provider.policy(workspace(), profile())["actor_id"], 25)
        self.assertEqual(len(self.provider.checks(workspace(), 9, profile())), 1)

    def test_admin_bypass_is_rejected_even_with_green_checks(self):
        self.provider.bypass = [
            {"actor_type": "RepositoryRole", "actor_id": 5, "bypass_mode": "always"}
        ]
        with self.assertRaises(DeliveryError):
            self.provider.policy(workspace(), profile())

    def test_empty_enforced_checks_are_rejected(self):
        self.provider.rules[0]["parameters"]["required_status_checks"] = []
        with self.assertRaises(DeliveryError):
            self.provider.policy(workspace(), profile())

    def test_check_from_wrong_producer_is_rejected(self):
        self.provider.check["app"]["id"] = 999
        with self.assertRaises(DeliveryError):
            self.provider.checks(workspace(), 9, profile())

    def test_duplicate_forged_check_cannot_hide_behind_success(self):
        self.provider.checks_extra = [deepcopy(self.provider.check)]
        self.provider.checks_extra[0]["app"]["id"] = 999
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

    def test_historical_check_confirmation_survives_github_removing_merged_pr_association(
        self,
    ):
        self.provider.workflow["pull_requests"] = []
        captured = receipt()["checks"]
        self.assertEqual(
            self.provider.confirm_checks(workspace(), profile(), captured), captured
        )

    def test_historical_confirmation_still_rejects_wrong_head(self):
        self.provider.workflow["head_sha"] = "e" * 40
        with self.assertRaises(DeliveryError):
            self.provider.confirm_checks(workspace(), profile(), receipt()["checks"])

    def test_changed_required_workflow_cannot_select_its_own_policy(self):
        self.provider.content = lambda repo, path, revision: (
            b"trusted" if revision == "a" * 40 else b"candidate skips tests"
        )
        with self.assertRaises(DeliveryError):
            self.provider.policy(workspace(), profile())

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


class MergeHost:
    def __init__(self):
        self.run_id = str(uuid4())
        self.policy = ExecutionPolicy(delivery_mode="merge")
        self.snapshot = {"effective": {"merge_profile": profile()}}
        self.native = {
            "attempt": 0,
            "deadline": (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat(),
            "workspace_id": str(uuid4()),
            "context": {
                "id": str(uuid4()),
                "digest": "d" * 64,
                "document": {
                    "source": {"issue_number": 1, "metadata": {"issue_id": 100}}
                },
            },
            "delivery": {
                "publication_id": str(uuid4()),
                "workspace": workspace(),
                "pull_request": {
                    "repository_id": 17,
                    "number": 9,
                    "url": "https://github.com/owner/repo/pull/9",
                    "base": "main",
                    "head": "task/1",
                    "head_sha": "b" * 40,
                },
            },
        }
        self.records = []

    def checkpoint(self, **kwargs):
        return True

    def save(self):
        pass

    def context_binding(self):
        return {
            "context_id": self.native["context"]["id"],
            "context_digest": "d" * 64,
            "policy_digest": self.policy.digest,
            "attempt": 0,
        }

    def intent(self, payload, key):
        self.records.append((key, payload))
        return str(uuid4())

    def artifact(self, kind, payload, key):
        self.records.append((kind, payload))

    def status(self, *args, **kwargs):
        pass


class MergeLifecycleTests(unittest.TestCase):
    def test_base_change_after_paused_unstarted_intent_releases_only_that_fence(self):
        host = MergeHost()
        host.native["candidate"] = {"workspace": workspace()}
        identity = str(uuid4())
        host.native["merge"] = {
            "profile": profile(),
            "receipt": receipt(host.run_id),
            "intents": {"merge": identity},
            "authorization_id": str(uuid4()),
        }
        host.secret = lambda *args: {"material": {"secret": "fixture"}}
        host.read = lambda key, operation: operation()
        provider = Provider()
        provider.pull = lambda *args: provider.raw
        provider.base_sha = lambda *args: "e" * 40
        with (
            patch.object(NativeMerge, "client", return_value=provider),
            patch("vibearr_runner.native_revision.MergeGitHub", return_value=provider),
        ):
            self.assertEqual(NativeMerge(host).step(), "continue")
        self.assertEqual(
            host.records[0],
            (
                "native_merge_terminal",
                {"schema_version": 1, "intent_id": identity, "state": "not_started"},
            ),
        )
        self.assertEqual(host.native["phase"], "publication_revision")
        self.assertEqual(host.native["publication_revision"]["intent"]["attempt"], 1)
        self.assertFalse(host.native["merge"].get("dispatched", False))

    def test_paused_dispatched_intent_never_becomes_unstarted(self):
        host = MergeHost()
        host.native["merge"] = {"dispatched": True, "intents": {"merge": str(uuid4())}}
        host.checkpoint = lambda **kwargs: False
        provider = Provider()
        provider.pull = lambda *args: provider.raw
        with patch.object(NativeMerge, "client", return_value=provider):
            with self.assertRaises(ReconciliationRequired):
                NativeMerge(host).step()
        self.assertEqual(host.records, [])

    def test_base_race_rejected_by_server_enters_revision_without_merge(self):
        from vibearr_runner.native_lifecycle import NativeMetadataRejected

        host = MergeHost()
        provider = Provider()
        provider.pull = lambda *args: provider.raw
        bases = iter(["a" * 40, "a" * 40, "e" * 40])
        provider.base_sha = lambda *args: next(bases)
        provider.reviews = lambda *args, **kwargs: receipt(host.run_id)["reviews"]

        def rejected(*args):
            raise NativeMetadataRejected("Confirmed absent after base race")

        host.intent = rejected
        with (
            patch.object(NativeMerge, "client", return_value=provider),
            patch.object(NativeMerge, "repair", return_value="continue") as repair,
        ):
            self.assertEqual(NativeMerge(host).step(), "continue")
        self.assertEqual(repair.call_args.args[0], "base_update")
        self.assertFalse(host.native["merge"].get("dispatched", False))

    def test_lost_merge_response_never_repeats_mutation_while_provider_open(self):
        host = MergeHost()
        provider = Provider()
        provider.pull = lambda *args: provider.raw
        provider.base_sha = lambda *args: "a" * 40
        provider.reviews = lambda *args, **kwargs: receipt(host.run_id)["reviews"]
        mutations = []

        def merge_once(*args):
            mutations.append(args)
            raise DeliveryUnknown("502 response lost")

        provider.merge_once = merge_once
        with patch.object(NativeMerge, "client", return_value=provider):
            self.assertEqual(NativeMerge(host).step(), "continue")
            host.native["deadline"] = "2000-01-01T00:00:00+00:00"
            with self.assertRaises(ReconciliationRequired):
                NativeMerge(host).step()
        self.assertEqual(len(mutations), 1)
        self.assertTrue(host.native["merge"]["dispatched"])

    def test_actual_merge_after_response_loss_is_recorded_before_completion(self):
        host = MergeHost()
        merge = NativeMerge(host)
        merge.data.update(
            dispatched=True,
            intents={"merge": str(uuid4())},
            profile=profile(),
            receipt=receipt(host.run_id),
        )
        provider = Provider()
        provider.raw.update(
            merged=True,
            state="closed",
            merge_commit_sha="e" * 40,
            merged_at="2026-11-16T01:10:00Z",
        )
        provider.pull = lambda *args: provider.raw
        with (
            patch.object(NativeMerge, "client", return_value=provider),
            patch.object(NativeMerge, "complete", return_value="continue"),
        ):
            self.assertEqual(merge.reconcile(), "continue")
        self.assertEqual(host.records[0][0], "native_merge")
        self.assertEqual(host.records[0][1]["merge"]["commit_sha"], "e" * 40)

    def test_unknown_dispatched_merge_cannot_terminate(self):
        host = MergeHost()
        host.native["merge"] = {"dispatched": True, "intents": {"merge": str(uuid4())}}
        with self.assertRaises(ReconciliationRequired):
            NativeMerge(host).terminal()

    def test_ci_timeout_prevents_merge_dispatch(self):
        host = MergeHost()
        host.native["ci_deadline"] = "2000-01-01T00:00:00+00:00"
        with patch.object(NativeMerge, "client") as provider:
            with self.assertRaises(DeliveryError):
                NativeMerge(host).step()
        provider.assert_not_called()

    def test_unstarted_merge_intent_can_resolve_before_cancellation(self):
        host = MergeHost()
        identity = str(uuid4())
        host.native["merge"] = {"intents": {"merge": identity}}
        NativeMerge(host).terminal()
        self.assertEqual(
            host.records[0],
            (
                "native_merge_terminal",
                {"schema_version": 1, "intent_id": identity, "state": "not_started"},
            ),
        )


class CoordinatorTests(unittest.TestCase):
    def test_merge_restart_waypoints_distinguish_actual_provider_effects(self):
        self.assertEqual(
            merge_waypoint({"merge": {"dispatched": True}}), "merge-dispatched"
        )
        self.assertEqual(
            merge_waypoint(
                {"merge": {"dispatched": True, "merge": {"commit_sha": "e" * 40}}}
            ),
            "merge-observed",
        )
        self.assertEqual(
            merge_waypoint({"merge": {"comment_dispatched": True}}),
            "completion-comment",
        )
        self.assertEqual(
            merge_waypoint({"merge": {"close_dispatched": True}}), "completion-close"
        )
        self.assertEqual(
            merge_waypoint({"merge": {"completed": True}}, "reporting-pending"),
            "reporting-pending",
        )

    def test_lost_status_response_is_reconciled_without_republication(self):
        value = receipt()["reviews"][0]
        payload = {
            "state": "success",
            "context": value["context"],
            "target_url": value["report_url"],
            "description": "Trusted independent AI review: no P2 or higher findings",
        }
        provider = Provider()
        provider.pages = lambda *args: []
        writes = []

        def request(*args):
            writes.append(args)
            raise DeliveryUnknown("lost status response")

        provider.request = request
        binding = {
            "run_id": str(uuid4()),
            "context_digest": "d" * 64,
            "workspace": workspace(),
            "pull_number": 9,
        }
        with (
            tempfile.TemporaryDirectory() as directory,
            patch("vibearr_runner.merge_review.review_status", return_value=payload),
        ):
            state = Path(directory) / "review.json"
            arguments = {
                "profile": profile(),
                "binding": binding,
                "role": "independent",
                "report_url": value["report_url"],
                "state_file": state,
                "apply": True,
            }
            with self.assertRaises(DeliveryUnknown):
                publish_review(provider, **arguments)
            with self.assertRaises(ReconciliationRequired):
                publish_review(provider, **arguments)
            self.assertEqual(len(writes), 1)
            provider.pages = lambda *args: [
                {**payload, "id": 500, "creator": {"id": 25}}
            ]
            self.assertEqual(publish_review(provider, **arguments)["status_id"], 500)
            self.assertEqual(len(writes), 1)

    def test_review_preview_creates_no_checkpoint_or_status(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch(
                "vibearr_runner.merge_review.review_status",
                return_value={"state": "success"},
            ),
        ):
            state = Path(directory) / "review.json"
            provider = Provider()
            result = publish_review(
                provider,
                profile=profile(),
                binding={},
                role="root",
                report_url="url",
                state_file=state,
            )
            self.assertFalse(result["apply"])
            self.assertFalse(state.exists())
            self.assertEqual(provider.requests, [])

    def test_setup_preview_does_not_modify_policy_or_checkpoint(self):
        plan = {
            "repository_id": 17,
            "repository": "owner/repo",
            "base": "main",
            "ruleset_id": 5,
            "original": {"bypass_actors": [5]},
            "desired": {"bypass_actors": []},
        }
        provider = Provider()
        with (
            tempfile.TemporaryDirectory() as directory,
            patch("vibearr_runner.merge_setup.setup_plan", return_value=plan),
        ):
            state = Path(directory) / "setup.json"
            result = apply_setup(
                provider, profile(), base="main", ruleset_id=5, checkpoint=state
            )
            self.assertFalse(result["apply"])
            self.assertFalse(state.exists())
            self.assertEqual(provider.requests, [])

    def test_unknown_setup_update_does_not_repeat_ruleset_mutation(self):
        plan = {
            "repository_id": 17,
            "repository": "owner/repo",
            "base": "main",
            "ruleset_id": 5,
            "original": {"bypass_actors": [5]},
            "desired": {"bypass_actors": []},
        }
        provider = Provider()
        writes = []

        def request(*args):
            writes.append(args)
            raise DeliveryUnknown("lost rules update")

        provider.request = request
        with (
            tempfile.TemporaryDirectory() as directory,
            patch("vibearr_runner.merge_setup.setup_plan", return_value=plan),
        ):
            state = Path(directory) / "setup.json"
            with self.assertRaises(DeliveryUnknown):
                apply_setup(
                    provider,
                    profile(),
                    base="main",
                    ruleset_id=5,
                    checkpoint=state,
                    apply=True,
                )
            with self.assertRaises(ReconciliationRequired):
                apply_setup(
                    provider,
                    profile(),
                    base="main",
                    ruleset_id=5,
                    checkpoint=state,
                    apply=True,
                )
            self.assertEqual(len(writes), 1)
            self.assertEqual(
                json.loads(state.read_bytes())["plan"]["original"], plan["original"]
            )


if __name__ == "__main__":
    unittest.main()
