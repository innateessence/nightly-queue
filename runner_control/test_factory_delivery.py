from copy import deepcopy
from datetime import timedelta
from unittest.mock import patch
from uuid import uuid4

from django.test import TestCase, override_settings
from django.utils import timezone

from credentials.test_models import TEST_KEY
from work_queue.models import MergeEvidence, QueueItem
from work_queue.services import enqueue_item

from .factory_delivery import validate_factory_workspace
from .models import Run
from .publication_observations import (
    claim_observation,
    finish_observation,
    observe_delivered_publications,
    observe_provider,
    observe_run,
    provider,
)
from .secrets import secret_document
from .services import RunConflict, claim_run, finish_run
from .settings_models import RunnerSettings
from .configuration import configuration_document
from .test_native_protocol import NativeProtocolMixin


class Provider:
    def __init__(self, workspace, pull, *, merged=False, state="open"):
        self.workspace, self.reference = workspace, pull
        self.calls = []
        self.raw = {
            "number": pull["number"],
            "html_url": pull["url"],
            "state": state,
            "head": {
                "repo": {"id": 123, "full_name": workspace["repository"]},
                "ref": workspace["branch"],
                "sha": workspace["commit_sha"],
            },
            "base": {
                "repo": {"id": 123, "full_name": workspace["repository"]},
                "ref": workspace["base_branch"],
            },
            "merged": merged,
            "merge_commit_sha": "e" * 40,
            "merged_at": "2026-11-22T12:00:00Z",
            "merged_by": {"id": 1234, "login": "human-reviewer", "type": "User"},
        }

    def repository(self, **kwargs):
        self.calls.append(("GET", "/repositories/123"))
        return {"repository_id": 123, "repository": self.workspace["repository"]}, {}

    def pull(self, repository, number):
        self.calls.append(("GET", f"/repos/{repository}/pulls/{number}"))
        return deepcopy(self.raw)

    def base_sha(self, workspace):
        self.calls.append(("GET", "/base"))
        return "f" * 40

    def request(self, method, path):
        self.calls.append((method, path))
        if method != "GET":
            raise AssertionError("Observer attempted a provider write")
        before, _ = path.rsplit("/", 1)[1].split("...")
        return {
            "status": "ahead",
            "base_commit": {"sha": before},
            "merge_base_commit": {"sha": before},
        }


@override_settings(VIBEARR_CREDENTIAL_KEY=TEST_KEY)
class PublicationObservationTests(NativeProtocolMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.complete_delivery()
        self.write("verified_delivery", self.evidence)
        self.terminate()
        finish_run(
            runner=self.runner,
            run_id=self.run.pk,
            claim_token=self.claim,
            status="succeeded",
            result_url=self.pull["url"],
            outcome="delivered",
        )
        self.run.refresh_from_db()
        self.item.refresh_from_db()
        self.current_credential = self.github
        self.auth = (self.github.pk, self.github.secret_version)
        self.provider = Provider(self.workspace, self.pull)

    def observe(self):
        with patch(
            "runner_control.publication_observations.provider",
            return_value=(self.provider, self.auth),
        ):
            return observe_run(self.run.pk)

    def make_due(self):
        self.run.refresh_from_db()
        status = self.run.native_status
        status.get("publication_observation", {}).pop("next_attempt_at", None)
        Run.objects.filter(pk=self.run.pk).update(native_status=status)

    def issue_source(self):
        QueueItem.objects.filter(pk=self.item.pk).update(
            source="github", issue_number=1, github_repository_id=123
        )
        snapshot = {**self.run.source_snapshot, "repository_id": 123, "issue_number": 1}
        Run.objects.filter(pk=self.run.pk).update(source_snapshot=snapshot)
        self.run.refresh_from_db()
        self.item.refresh_from_db()

    def test_delivery_releases_claim_and_independent_task_can_be_claimed(self):
        self.assertEqual(self.item.status, QueueItem.Status.SUCCEEDED)
        self.assertIsNone(self.run.active_slot)
        independent = enqueue_item(
            project=self.project, title="Independent", instructions="Other work"
        )
        next_run = claim_run(
            runner=self.runner,
            run_id=uuid4(),
            configuration_revision=configuration_document(self.runner)["revision"],
        )
        self.assertEqual(next_run.item_id, independent.pk)
        self.assertEqual(next_run.state, Run.State.ACTIVE)

    def test_current_read_provider_works_after_completion_without_claim(self):
        selected, _ = provider(self.run)
        self.assertTrue(selected.read_only)
        with self.assertRaises(RunConflict):
            secret_document(
                {
                    "scope": "run",
                    "run_id": str(self.run.pk),
                    "claim_token": str(self.claim),
                    "purpose": "repository",
                },
                self.runner,
            )

    def test_open_pr_records_awaiting_human_review_without_merge_evidence(self):
        self.assertEqual(self.observe()["status"], "awaiting_review")
        self.assertFalse(MergeEvidence.objects.exists())
        observation = self.run.evidence.get(kind="publication_observation").payload
        self.assertIsNone(observation["merged_by"])
        self.assertTrue(all(method == "GET" for method, _ in self.provider.calls))

    def test_closed_unmerged_pr_does_not_unlock_dependency(self):
        self.issue_source()
        self.provider.raw["state"] = "closed"
        self.assertEqual(self.observe()["status"], "closed_unmerged")
        self.assertFalse(MergeEvidence.objects.exists())

    def test_human_merge_records_actor_time_and_verified_ancestry_once(self):
        self.issue_source()
        self.provider.raw.update(merged=True, state="closed")
        self.assertEqual(self.observe()["status"], "merged")
        record = MergeEvidence.objects.get(repository_id=123, issue_number=1)
        self.assertEqual(record.merge_sha, "e" * 40)
        observation = self.run.evidence.get(kind="publication_observation").payload
        self.assertEqual(observation["merged_by"]["id"], 1234)
        self.assertEqual(observation["merged_at"], "2026-11-22T12:00:00+00:00")
        self.assertEqual(len(observation["ancestry_proof"]), 2)
        self.assertIsNone(self.observe())
        self.assertEqual(
            self.run.evidence.filter(kind="publication_observation").count(), 1
        )

    def test_bot_merge_does_not_unlock_dependency(self):
        self.issue_source()
        self.provider.raw.update(merged=True, state="closed")
        self.provider.raw["merged_by"]["type"] = "Bot"
        self.assertEqual(self.observe()["status"], "blocked")
        self.assertFalse(MergeEvidence.objects.exists())

    def test_changed_head_is_not_the_delivered_publication(self):
        self.provider.raw["head"]["sha"] = "9" * 40
        self.assertEqual(self.observe()["status"], "blocked")
        self.assertFalse(
            self.run.evidence.filter(kind="publication_observation").exists()
        )

    def test_unverified_ancestry_does_not_record_human_merge(self):
        self.issue_source()
        self.provider.raw.update(merged=True, state="closed")
        self.provider.request = lambda *args: {
            "status": "diverged",
            "base_commit": {"sha": "a" * 40},
        }
        self.assertEqual(self.observe()["status"], "blocked")
        self.assertFalse(MergeEvidence.objects.exists())

    def test_current_credential_revocation_blocks_finished_claim_observation(self):
        type(self.github).objects.filter(pk=self.github.pk).update(
            revoked_at=timezone.now()
        )
        self.assertEqual(observe_run(self.run.pk)["status"], "blocked")
        self.assertFalse(
            self.run.evidence.filter(kind="publication_observation").exists()
        )

    def test_authorization_changed_during_provider_read_cannot_commit_observation(self):
        claimed, token = claim_observation(self.run.pk, timezone.now())
        observed = observe_provider(claimed, self.provider)
        type(self.github).objects.filter(pk=self.github.pk).update(
            secret_version=self.github.secret_version + 1
        )
        with self.assertRaises(RunConflict):
            finish_observation(self.run.pk, token, observed, self.auth)
        self.assertFalse(
            self.run.evidence.filter(kind="publication_observation").exists()
        )

    def test_observer_resumes_after_lost_lease_without_rebuilding_delivery(self):
        claimed, _ = claim_observation(
            self.run.pk, timezone.now() - timedelta(minutes=3)
        )
        self.assertIsNotNone(claimed)
        self.assertEqual(self.observe()["status"], "awaiting_review")
        self.run.refresh_from_db()
        self.assertEqual(self.run.native_outcome, "delivered")
        self.assertIsNone(self.run.active_slot)

    def test_unchanged_provider_observation_does_not_append_duplicate_event(self):
        self.observe()
        self.make_due()
        self.observe()
        self.assertEqual(
            self.run.evidence.filter(kind="publication_observation").count(), 1
        )

    def test_observer_batch_uses_existing_scheduler_and_paused_processing_is_irrelevant(
        self,
    ):
        RunnerSettings.objects.filter(pk=1).update(paused=True)
        with patch(
            "runner_control.publication_observations.provider",
            return_value=(self.provider, self.auth),
        ):
            self.assertEqual(
                observe_delivered_publications()[0]["status"], "awaiting_review"
            )

    def test_observer_never_resurrects_a_cancelled_run(self):
        Run.objects.filter(pk=self.run.pk).update(
            native_outcome="canceled", result_status="failed"
        )
        self.assertIsNone(self.observe())
        self.assertFalse(
            self.run.evidence.filter(kind="publication_observation").exists()
        )

    def test_stale_observer_cannot_overwrite_reclaimed_lease(self):
        _, old = claim_observation(self.run.pk, timezone.now() - timedelta(minutes=3))
        _, current = claim_observation(self.run.pk, timezone.now())
        self.assertNotEqual(old, current)
        with self.assertRaises(RunConflict):
            finish_observation(self.run.pk, old)

    def test_expired_observer_cannot_commit_a_successful_read(self):
        claimed, token = claim_observation(
            self.run.pk, timezone.now() - timedelta(minutes=3)
        )
        observed = observe_provider(claimed, self.provider)
        with self.assertRaises(RunConflict):
            finish_observation(self.run.pk, token, observed, self.auth)

    def test_foreign_repository_cannot_unlock_dependencies(self):
        self.provider.raw["base"]["repo"]["id"] = 999
        self.assertEqual(self.observe()["status"], "blocked")
        self.assertFalse(MergeEvidence.objects.exists())

    def test_issue_closure_alone_cannot_unlock_dependencies(self):
        self.issue_source()
        QueueItem.objects.filter(pk=self.item.pk).update(issue_state="closed")
        self.assertEqual(self.observe()["status"], "awaiting_review")
        self.assertFalse(MergeEvidence.objects.exists())

    def test_late_merge_after_closed_unmerged_pr_is_observed_without_new_claim(self):
        self.issue_source()
        self.provider.raw["state"] = "closed"
        self.assertEqual(self.observe()["status"], "closed_unmerged")
        self.make_due()
        self.provider.raw["merged"] = True
        self.assertEqual(self.observe()["status"], "merged")
        self.run.refresh_from_db()
        self.assertIsNone(self.run.active_slot)
        self.assertEqual(self.run.native_outcome, "delivered")
        self.assertEqual(
            self.run.evidence.filter(kind="publication_observation").count(), 2
        )

    def test_provider_merge_changed_during_confirmation_is_not_committed(self):
        self.issue_source()
        self.provider.raw.update(merged=True, state="closed")
        original = self.provider.pull
        calls = []

        def changed(repository, number):
            result = original(repository, number)
            calls.append(number)
            if len(calls) == 2:
                result["merge_commit_sha"] = "9" * 40
            return result

        self.provider.pull = changed
        self.assertEqual(self.observe()["status"], "blocked")
        self.assertFalse(MergeEvidence.objects.exists())

    def test_observation_conflict_preserves_the_previous_verified_merge(self):
        from work_queue.services import record_verified_merge

        self.issue_source()
        now = timezone.now()
        record_verified_merge(
            repository_id=123,
            issue_number=1,
            pull_number=99,
            merge_sha="9" * 40,
            merged_at=now,
            provenance="prior",
            operation_id="prior",
        )
        self.provider.raw.update(merged=True, state="closed")
        self.assertEqual(self.observe()["status"], "blocked")
        self.assertEqual(MergeEvidence.objects.get().pull_number, 99)
        self.assertFalse(
            self.run.evidence.filter(kind="publication_observation").exists()
        )

    def test_new_current_project_credential_is_used_after_completed_claim(self):
        RunnerSettings.objects.filter(pk=1).update(repository_credential_id=None)
        self.assertEqual(observe_run(self.run.pk)["status"], "blocked")
        self.assertFalse(
            self.run.evidence.filter(kind="publication_observation").exists()
        )

    def test_dependency_stays_blocked_until_verified_human_merge(self):
        from work_queue.services import dependency_wait_reason

        self.issue_source()
        dependent = enqueue_item(
            project=self.project, title="Dependent", instructions="Needs issue one"
        )
        QueueItem.objects.filter(pk=dependent.pk).update(
            source="github", issue_number=2, github_repository_id=123, dependencies=[1]
        )
        dependent.refresh_from_db()
        self.assertTrue(dependency_wait_reason(dependent))
        self.observe()
        self.assertTrue(dependency_wait_reason(dependent))
        self.make_due()
        self.provider.raw.update(merged=True, state="closed")
        self.assertEqual(self.observe()["status"], "merged")
        self.assertEqual(dependency_wait_reason(dependent), "")

    def test_active_runner_cannot_forge_trusted_observation(self):
        other = enqueue_item(
            project=self.project, title="Second", instructions="Other work"
        )
        run = claim_run(
            runner=self.runner,
            run_id=uuid4(),
            configuration_revision=configuration_document(self.runner)["revision"],
        )
        other.refresh_from_db()
        from .services import record_evidence

        with self.assertRaises(RunConflict):
            record_evidence(
                runner=self.runner,
                run_id=run.pk,
                claim_token=other.claim_token,
                operation_id=uuid4(),
                kind="publication_observation",
                payload={"status": "merged"},
            )

    def test_enabled_factory_without_saved_review_chain_cannot_finish_delivery(self):
        self.run.configuration_snapshot["effective"]["factory"] = {"enabled": True}
        with self.assertRaisesMessage(
            RunConflict, "current saved factory specification"
        ):
            validate_factory_workspace(self.run, self.workspace, self.context)
