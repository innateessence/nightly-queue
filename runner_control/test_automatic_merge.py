import copy
import hashlib
from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from threading import Barrier
from unittest.mock import Mock, patch
from uuid import UUID, uuid4, uuid5

from django.core.exceptions import ValidationError
from django.db import connection, connections
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from credentials.test_models import TEST_KEY
from projects.models import Project
from vibearr_runner import native_contract as contract
from vibearr_runner.delivery import DeliveryError
from vibearr_executor.artifacts import pack
from work_queue.models import MergeEvidence, QueueItem

from .configuration import configuration_document
from .merge_services import provider_completed, provider_merged, reserve_fence
from .models import RepositoryMergeFence, Run, RunnerIdentity
from .native_services import authorize, check_workspace
from .secrets import secret_document
from .services import RunConflict, finish_run
from .settings_models import RunnerSettings
from .test_native_protocol import NativeProtocolMixin


def trusted_profile(repository_id=123):
    return {
        "schema_version": 1,
        "repository_id": repository_id,
        "actor_id": 456,
        "checks": [
            {
                "name": "Validate",
                "app_id": 15368,
                "workflow_id": 22,
                "workflow_path": ".github/workflows/test.yml",
                "workflow_digest": "a" * 64,
            }
        ],
        "reviews": [
            {
                "role": role,
                "context": f"ai/{role}",
                "creator_id": 456,
                "report_repository": "example/control",
                "report_repository_id": 789,
                "report_prefix": "reviews/",
            }
            for role in ("independent", "root")
        ],
        "ci_timeout_seconds": 1800,
        "delete_branch": False,
    }


class MergeProtocolMixin(NativeProtocolMixin):
    def setUp(self):
        super().setUp()
        saved = RunnerSettings.objects.get(pk=1)
        saved.execution_policy["delivery_mode"] = "merge"
        saved.merge_profile = trusted_profile()
        saved.save()
        document = configuration_document()
        selected = next(
            entry
            for entry in document["projects"]
            if entry["project_id"] == self.project.pk
        )
        snapshot = {
            **self.run.configuration_snapshot,
            **selected,
            "global": document["global"],
            "revision": document["revision"],
        }
        self.run.configuration_snapshot = snapshot
        self.run.configuration_revision = document["revision"]
        self.run.save(
            update_fields=["configuration_snapshot", "configuration_revision"]
        )

    def github_source(self):
        now = timezone.now()
        Project.objects.filter(pk=self.project.pk).update(
            autonomous=True, enabled_at=now - timedelta(days=1)
        )
        metadata = {
            "issue_id": 101,
            "title": self.item.title,
            "body": self.item.instructions,
        }
        QueueItem.objects.filter(pk=self.item.pk).update(
            source="github",
            issue_number=1,
            github_issue_id=101,
            github_repository_id=123,
            author_id=456,
            issue_created_at=now,
            issue_updated_at=now,
            issue_state="open",
            issue_revision="a" * 64,
            source_metadata=metadata,
            branch_name="vibearr/issue-1",
        )
        self.item.refresh_from_db()
        self.run.source_snapshot = {
            "repository_id": 123,
            "issue_number": 1,
            "title": self.item.title,
            "body": self.item.instructions,
            "metadata": metadata,
            "revision": "a" * 64,
            "comments": [],
        }
        self.run.save(update_fields=["source_snapshot"])

    def prepare_context(self):
        self.run.source_snapshot["repository_id"] = 123
        self.run.save(update_fields=["source_snapshot"])
        with patch("runner_control.test_native_protocol.refresh_issue_for_launch"):
            super().prepare_context()

    def receipt(self):
        reviews = []
        for role in ("independent", "root"):
            reviews.append(
                {
                    "role": role,
                    "status_id": 20 + len(reviews),
                    "creator_id": 456,
                    "context": f"ai/{role}",
                    "report_url": f"https://github.com/example/control/blob/{'a' * 40}/reviews/{role}.json",
                    "report_digest": "a" * 64,
                    "report": {
                        "schema_version": 1,
                        "role": role,
                        "run_id": str(self.run.pk),
                        "context_digest": self.context.digest,
                        "repository_id": 123,
                        "pull_number": 9,
                        "head_sha": self.workspace["commit_sha"],
                        "base_sha": self.workspace["base_revision"],
                        "tree_sha": self.workspace["tree_sha"],
                        "session_id": f"ses-{role}",
                        "reviewer": f"AI {role}",
                        "findings": [],
                        "verdict": "no_p2_or_higher",
                    },
                }
            )
        return {
            "policy": {
                "actor_id": 456,
                "profile_digest": contract.digest(trusted_profile()),
                "rulesets": [{"id": 10, "digest": "a" * 64}],
            },
            "checks": [
                {
                    "name": "Validate",
                    "producer_id": 15368,
                    "head_sha": self.workspace["commit_sha"],
                    "conclusion": "success",
                    "url": "https://github.com/example/configured/actions/runs/3/job/4",
                    "check_id": 4,
                    "workflow_id": 22,
                    "run_id": 3,
                    "run_attempt": 1,
                    "suite_id": 5,
                }
            ],
            "reviews": reviews,
        }

    def merge_intent(self, stage="merge"):
        publication = self.run.evidence.filter(kind="native_delivery").last().payload
        return contract.intent_document(
            "merge",
            **self.common(),
            data={
                "publication_id": publication["publication_id"],
                "workspace_id": str(self.workspace_entry.operation_id),
                "workspace": self.workspace,
                "pull_request": self.pull,
                "stage": stage,
                "profile": trusted_profile(),
                "receipt": self.receipt(),
            },
        )

    def start_merge(self):
        self.complete_delivery()
        payload = self.merge_intent()
        with patch(
            "runner_control.merge_services.provider_authorization",
            return_value=payload["data"]["receipt"],
        ):
            self.merge_intent_entry = self.write("native_intent", payload)
        self.intent_ids.append(str(self.merge_intent_entry.operation_id))

    def complete_merge(self):
        self.start_merge()
        self.merged = {
            "schema_version": 1,
            "intent_id": str(self.merge_intent_entry.operation_id),
            "workspace_id": str(self.workspace_entry.operation_id),
            "publication_id": self.merge_intent_entry.payload["data"]["publication_id"],
            "workspace": self.workspace,
            "pull_request": self.pull,
            "receipt": self.receipt(),
            "merge": {
                "commit_sha": "6" * 40,
                "head_sha": self.workspace["commit_sha"],
                "merged_at": timezone.now().isoformat(),
            },
        }
        with patch("runner_control.merge_services.provider_merged"):
            self.write("native_merge", self.merged)

    def complete_reporting(self):
        self.complete_merge()
        intents = []
        for stage in ("comment", "close"):
            entry = self.write("native_intent", self.merge_intent(stage))
            intents.append(str(entry.operation_id))
            self.intent_ids.append(str(entry.operation_id))
        self.completion = {
            "schema_version": 1,
            "intent_ids": intents,
            "merge_intent_id": str(self.merge_intent_entry.operation_id),
            "issue_number": 1,
            "issue_id": 101,
            "comment_id": 77,
            "marker": f"<!-- vibearr-run:{self.run.pk}:complete -->",
            "state": "closed",
            "branch_deleted": False,
        }
        with patch("runner_control.merge_services.provider_completed"):
            self.completion_entry = self.write("native_completion", self.completion)

    def final_evidence(self):
        value = copy.deepcopy(self.evidence)
        value["delivery"].update(
            mode="merge",
            checks=[
                {
                    key: entry[key]
                    for key in ("name", "producer_id", "head_sha", "conclusion", "url")
                }
                for entry in self.merged["receipt"]["checks"]
            ],
            merge={
                key: self.merged["merge"][key] for key in ("commit_sha", "head_sha")
            },
        )
        if self.item.source == "github":
            close_intent = next(
                identity
                for identity in self.completion["intent_ids"]
                if self.run.evidence.get(operation_id=identity).payload["data"]["stage"]
                == "close"
            )
            value["delivery"]["issue_completion"] = {
                "operation_id": close_intent,
                "issue_number": 1,
                "state": "closed",
            }
        return value

    def revision_intent(self, reason="base_update", attempt=1):
        publication = self.run.evidence.filter(kind="native_delivery").last().payload
        return contract.intent_document(
            "revision",
            **{**self.common(), "attempt": attempt},
            data={
                "publication_id": publication["publication_id"],
                "workspace_id": str(self.workspace_entry.operation_id),
                "workspace": self.workspace,
                "pull_request": self.pull,
                "base_revision": (
                    "b" * 40
                    if reason == "base_update"
                    else self.workspace["base_revision"]
                ),
                "reason": reason,
            },
        )

    def complete_revision(self):
        self.complete_delivery()
        value = self.revision_intent()
        with patch("runner_control.merge_services.provider_revision"):
            intent = self.write("native_intent", value)
        self.intent_ids.append(str(intent.operation_id))
        revised = {
            **self.workspace,
            "base_revision": "b" * 40,
            "commit_sha": "7" * 40,
            "tree_sha": "8" * 40,
        }
        result = {
            "schema_version": 1,
            "intent_id": str(intent.operation_id),
            "context_id": str(self.context.pk),
            "context_digest": self.context.digest,
            "attempt": 1,
            "previous_workspace_id": str(self.workspace_entry.operation_id),
            "agent_intent_id": str(self.agent_intent.operation_id),
            "workspace": revised,
            "source_digest": "9" * 64,
            "export_digest": self.workspace_entry.payload["export_digest"],
            "requirements_digest": self.profile.dependency_digest,
            "parents": [self.workspace["commit_sha"], "b" * 40],
        }
        self.revision_entry = self.write("native_revision", result)
        return result

    def local_validation(self, workspace, *, archive=None, finished=True):
        archive = self.changed if archive is None else archive
        candidate = workspace.payload
        session = copy.deepcopy(self.validation_intent.payload["data"])
        session.pop("workspace_id")
        session.pop("provenance")
        session.update(
            profile=self.profile.data(),
            attempt_id=str(uuid4()),
            source_digest=candidate["source_digest"],
            tree_sha=candidate["workspace"]["tree_sha"],
        )
        context_digest = contract.digest(
            {
                "schema_version": 1,
                "kind": "validation",
                "tree_sha": session["tree_sha"],
                "source_digest": session["source_digest"],
                "validation_profile_digest": self.profile.digest,
            }
        )
        for operation in session["operations"]:
            operation["manifest"].update(
                operation_id=str(uuid5(UUID(session["attempt_id"]), operation["id"])),
                source_digest=session["source_digest"],
                context_digest=context_digest,
            )
        intent = self.write(
            "native_intent",
            contract.validation_intent(
                **{**self.common(), "attempt": candidate["attempt"]},
                session=session,
                workspace_id=str(workspace.operation_id),
            ),
        )
        self.intent_ids.append(str(intent.operation_id))
        if not finished:
            return intent
        observations = [
            self.observed(
                operation["manifest"]["operation_id"],
                archive,
                archive,
                exit_code=1 if operation["id"] == "cli" else 0,
            )
            for operation in session["operations"]
        ]
        self.operations.extend(observations)
        return self.write(
            "native_validation",
            contract.validation_result(
                intent_id=str(intent.operation_id),
                session=session,
                operations=observations,
            ),
        )

    def repair_invocation(self, workspace, *, attempt=2, deadline=None):
        manifest = {
            **self.session.manifest,
            "operation_id": str(uuid4()),
            "source_digest": workspace.payload["source_digest"],
            "deadline": deadline or self.deadline,
        }
        session = replace(self.session, manifest=manifest)
        return contract.agent_intent(
            **{**self.common(), "attempt": attempt},
            session=session,
            workspace_id=str(workspace.operation_id),
        )

    def remote_repair_candidate(self):
        self.complete_delivery()
        revision = self.revision_intent(reason="ci_repair")
        with patch("runner_control.merge_services.provider_revision"):
            revision_entry = self.write("native_intent", revision)
        self.intent_ids.append(str(revision_entry.operation_id))
        payload = self.repair_invocation(self.workspace_entry, attempt=1)
        intent = self.write("native_intent", payload)
        self.intent_ids.append(str(intent.operation_id))
        session = replace(self.session, manifest=payload["data"]["session"]["manifest"])
        archive = pack(
            {
                "requirements.txt": (0o644, b"example==1.0\n"),
                "file.py": (0o644, b"repaired\n"),
            }
        )
        observed = self.observed(
            session.manifest["operation_id"], self.changed, archive
        )
        self.operations.append(observed)
        outcome = {
            "result": observed["result"],
            "artifact": observed["result"]["artifact"],
            "source": archive,
            "successful": True,
            "session_id": "ses-first-repair",
            "agent_version": session.agent_version,
        }
        self.write(
            "native_agent",
            contract.agent_result(
                intent_id=str(intent.operation_id),
                session=session,
                outcome=outcome,
                observed=observed,
            ),
        )
        source_digest = hashlib.sha256(archive).hexdigest()
        workspace = {**self.workspace, "commit_sha": "7" * 40, "tree_sha": "8" * 40}
        entry = self.write(
            "native_workspace",
            contract.workspace_record(
                context_id=str(self.context.pk),
                context_digest=self.context.digest,
                attempt=1,
                agent_intent_id=str(intent.operation_id),
                workspace=workspace,
                source_digest=source_digest,
                export_digest=source_digest,
                requirements_digest=self.profile.dependency_digest,
            ),
        )
        return entry, archive


@override_settings(NQ_CREDENTIAL_KEY=TEST_KEY)
class AutomaticMergeTests(MergeProtocolMixin, TestCase):
    def test_failed_local_validation_after_base_update_allows_remaining_agent_repair(
        self,
    ):
        self.complete_revision()
        validation = self.local_validation(self.revision_entry)
        self.assertEqual(validation.payload["state"], "failed")
        self.assertEqual(len(validation.payload["checks"]), 9)
        accepted = self.write(
            "native_intent", self.repair_invocation(self.revision_entry)
        )
        self.assertEqual(accepted.payload["attempt"], 2)
        self.assertEqual(
            accepted.payload["data"]["session"]["manifest"]["deadline"], self.deadline
        )
        self.assertEqual(
            self.run.evidence.filter(
                kind="native_intent", payload__kind="agent"
            ).count(),
            2,
        )
        self.assertFalse(
            self.run.evidence.filter(
                kind="native_intent", payload__kind="revision", payload__attempt=2
            ).exists()
        )

    def test_failed_local_validation_after_remote_ci_repair_allows_remaining_repair(
        self,
    ):
        workspace, archive = self.remote_repair_candidate()
        validation = self.local_validation(workspace, archive=archive)
        cli = next(
            entry for entry in validation.payload["checks"] if entry["id"] == "cli"
        )
        self.assertEqual(cli["receipt"]["exit_code"], 1)
        accepted = self.write("native_intent", self.repair_invocation(workspace))
        self.assertEqual(accepted.payload["attempt"], 2)
        self.assertEqual(
            accepted.payload["data"]["session"]["manifest"]["deadline"], self.deadline
        )
        self.assertEqual(
            self.run.evidence.filter(
                kind="native_intent", payload__kind="agent"
            ).count(),
            3,
        )

    def test_unpublished_revision_cannot_start_repair_without_failed_validation(self):
        self.complete_revision()
        with self.assertRaisesMessage(RunConflict, "latest failed local validation"):
            self.write("native_intent", self.repair_invocation(self.revision_entry))

    def test_pending_latest_validation_cannot_reuse_older_local_failure(self):
        self.complete_revision()
        self.local_validation(self.revision_entry)
        self.local_validation(self.revision_entry, finished=False)
        with self.assertRaisesMessage(RunConflict, "latest failed local validation"):
            self.write("native_intent", self.repair_invocation(self.revision_entry))

    def test_published_candidate_still_requires_explicit_remote_repair_intent(self):
        self.complete_delivery()
        self.local_validation(self.workspace_entry)
        with self.assertRaisesMessage(
            RunConflict, "explicit publication revision intent"
        ):
            self.write(
                "native_intent", self.repair_invocation(self.workspace_entry, attempt=1)
            )

    def test_unpublished_local_repair_cannot_extend_original_agent_deadline(self):
        self.complete_revision()
        self.local_validation(self.revision_entry)
        deadline = (
            datetime.fromisoformat(self.deadline) + timedelta(seconds=60)
        ).isoformat()
        with self.assertRaisesMessage(RunConflict, "original stage deadline"):
            self.write(
                "native_intent",
                self.repair_invocation(self.revision_entry, deadline=deadline),
            )

    def test_local_repair_cannot_use_older_inspected_workspace(self):
        self.complete_revision()
        self.local_validation(self.revision_entry)
        with self.assertRaisesMessage(RunConflict, "latest inspected workspace"):
            self.write("native_intent", self.repair_invocation(self.workspace_entry))

    def test_malformed_intent_data_rejects_without_server_error(self):
        with self.assertRaises(ValidationError):
            self.write("native_intent", {"kind": "merge", "data": []})
        self.assertFalse(RepositoryMergeFence.objects.exists())

    def test_completion_flag_cannot_authorize_before_merge(self):
        with self.assertRaisesMessage(RunConflict, "provider-verified merge"):
            authorize(self.run, completion=True)

    def test_empty_list_merge_profile_is_not_an_unset_profile(self):
        saved = RunnerSettings.objects.get(pk=1)
        saved.merge_profile = []
        with self.assertRaises(ValidationError):
            saved.save()

    def test_merge_mode_without_trusted_profile_is_structurally_blocked(self):
        saved = RunnerSettings.objects.get(pk=1)
        saved.merge_profile = {}
        saved.save()
        project = configuration_document()["projects"][0]
        self.assertFalse(project["structurally_valid"])
        self.assertEqual(project["errors"][0]["field"], "merge_profile")

    def test_pending_repository_fence_blocks_another_merge(self):
        self.complete_delivery()
        RepositoryMergeFence.objects.create(
            repository_id=123,
            run=self.run,
            publication_id=uuid4(),
            intent_id=uuid4(),
            head_sha="f" * 40,
            base_sha="c" * 40,
        )
        value = self.merge_intent()
        with patch(
            "runner_control.merge_services.provider_authorization",
            return_value=value["data"]["receipt"],
        ):
            with self.assertRaisesMessage(RunConflict, "pending merge owns"):
                self.write("native_intent", value)

    def test_stale_claim_cannot_register_merge(self):
        self.complete_delivery()
        value = self.merge_intent()
        self.claim = uuid4()
        with self.assertRaises(RunConflict):
            self.write("native_intent", value)
        self.assertFalse(RepositoryMergeFence.objects.exists())

    def test_base_revision_preserves_original_paid_session_attribution(self):
        revision = self.complete_revision()
        check_workspace(self.run, revision["workspace"], self.context)
        self.assertEqual(
            revision["agent_intent_id"], str(self.agent_intent.operation_id)
        )
        self.assertEqual(self.run.evidence.filter(kind="native_agent").count(), 1)
        self.assertEqual(self.context.document["repository"]["base_revision"], "c" * 40)

    def test_revision_cannot_replace_its_merge_parents(self):
        revision = self.complete_revision()
        changed = copy.deepcopy(revision)
        changed["parents"][0] = "a" * 40
        with self.assertRaisesMessage(RunConflict, "saved ancestry"):
            self.write("native_revision", changed)

    def test_revision_cannot_publish_without_fresh_validation(self):
        revision = self.complete_revision()
        publication = self.run.evidence.filter(kind="native_delivery").last().payload
        intent = contract.delivery_intent(
            **{**self.common(), "attempt": 1},
            publication_id=publication["publication_id"],
            stage="push",
            workspace_id=str(self.revision_entry.operation_id),
            workspace=revision["workspace"],
        )
        with self.assertRaisesMessage(RunConflict, "validation must pass"):
            self.write("native_intent", intent)

    def test_base_only_revision_then_agent_repair_uses_shared_attempt_two(self):
        revision = self.complete_revision()
        self.workspace = revision["workspace"]
        self.workspace_entry = self.revision_entry
        publication = self.run.evidence.filter(kind="native_delivery").last().payload
        pull = dict(self.pull, head_sha=self.workspace["commit_sha"])
        self.run.evidence.create(
            operation_id=uuid4(),
            kind="native_delivery",
            payload={
                **publication,
                "workspace_id": str(self.revision_entry.operation_id),
                "workspace": self.workspace,
                "pushed_sha": self.workspace["commit_sha"],
                "pull_request": pull,
            },
        )
        self.pull = pull
        value = self.revision_intent(reason="ci_repair", attempt=2)
        with patch("runner_control.merge_services.provider_revision"):
            self.write("native_intent", value)
        manifest = {
            **self.session.manifest,
            "operation_id": str(uuid4()),
            "source_digest": revision["source_digest"],
        }
        session = replace(self.session, manifest=manifest)
        agent = contract.agent_intent(
            **{**self.common(), "attempt": 2},
            session=session,
            workspace_id=str(self.revision_entry.operation_id),
        )
        accepted = self.write("native_intent", agent)
        self.assertEqual(accepted.payload["attempt"], 2)
        self.assertEqual(
            accepted.payload["data"]["session"]["manifest"]["deadline"], self.deadline
        )

    def test_revision_after_merge_attempt_is_forbidden(self):
        self.start_merge()
        with self.assertRaisesMessage(RunConflict, "merge intent already owns"):
            self.write("native_intent", self.revision_intent())

    def test_pr_only_delivery_cannot_finish_merge_policy(self):
        self.complete_delivery()
        with self.assertRaisesMessage(RunConflict, "delivery mode"):
            self.write("verified_delivery", self.evidence)

    def test_merge_intent_acquires_repository_fence(self):
        self.start_merge()
        fence = RepositoryMergeFence.objects.get(repository_id=123)
        self.assertEqual(fence.run, self.run)
        self.assertEqual(
            str(fence.intent_id), str(self.merge_intent_entry.operation_id)
        )
        self.assertEqual(fence.state, "pending")

    def test_unresolved_merge_cannot_release_claim_after_cancel(self):
        self.start_merge()
        self.run.cancellation_requested_at = timezone.now()
        self.run.save(update_fields=["cancellation_requested_at"])
        with self.assertRaisesMessage(RunConflict, "outcome remains uncertain"):
            self.terminate()
        self.run.refresh_from_db()
        self.assertEqual(self.run.active_slot, 1)
        self.assertEqual(
            RepositoryMergeFence.objects.get(repository_id=123).state, "pending"
        )

    def test_merge_requires_current_provider_receipt(self):
        self.complete_delivery()
        value = self.merge_intent()
        observed = copy.deepcopy(value["data"]["receipt"])
        observed["checks"][0]["run_attempt"] = 2
        with patch(
            "runner_control.merge_services.provider_authorization",
            return_value=observed,
        ):
            with self.assertRaisesMessage(RunConflict, "Current provider"):
                self.write("native_intent", value)
        self.assertFalse(RepositoryMergeFence.objects.exists())

    def test_review_of_other_head_does_not_authorize_merge(self):
        self.complete_delivery()
        value = self.merge_intent()
        value["data"]["receipt"]["reviews"][0]["report"]["head_sha"] = "b" * 40
        with self.assertRaisesMessage(ValidationError, "exact candidate"):
            self.write("native_intent", value)

    def test_two_reviews_cannot_reuse_same_ai_session(self):
        self.complete_delivery()
        value = self.merge_intent()
        reviews = value["data"]["receipt"]["reviews"]
        reviews[1]["report"]["session_id"] = reviews[0]["report"]["session_id"]
        with self.assertRaisesMessage(RunConflict, "distinct sessions"):
            self.write("native_intent", value)

    def test_changed_merge_profile_fences_new_merge(self):
        self.complete_delivery()
        value = self.merge_intent()
        saved = RunnerSettings.objects.get(pk=1)
        saved.merge_profile["ci_timeout_seconds"] = 60
        saved.save()
        with self.assertRaisesMessage(RunConflict, "merge profile changed"):
            self.write("native_intent", value)

    def test_actual_merge_records_dependency_before_completion(self):
        self.github_source()
        self.complete_merge()
        record = MergeEvidence.objects.get(repository_id=123, issue_number=1)
        self.assertEqual(record.merge_sha, "6" * 40)
        self.assertEqual(record.provenance, "github:merge:v1")
        self.assertFalse(self.run.evidence.filter(kind="native_completion").exists())
        self.run.refresh_from_db()
        self.assertEqual(self.run.state, "active")

    def test_actual_issue_merge_cannot_release_claim_before_reporting(self):
        self.github_source()
        self.complete_merge()
        with self.assertRaisesMessage(RunConflict, "completed issue reporting"):
            self.terminate()
        self.run.refresh_from_db()
        self.assertEqual(self.run.active_slot, 1)

    def test_actual_merge_cannot_be_reported_as_failed(self):
        self.complete_merge()
        self.terminate()
        with self.assertRaisesMessage(RunConflict, "merged successful outcome"):
            finish_run(
                runner=self.runner,
                run_id=self.run.pk,
                claim_token=self.claim,
                status="failed",
                error_summary="lost completion",
                outcome="failed",
            )

    def test_merge_fact_replay_does_not_duplicate_dependency(self):
        self.github_source()
        self.complete_merge()
        entry = self.run.evidence.get(kind="native_merge")
        self.write("native_merge", self.merged, entry.operation_id)
        self.assertEqual(MergeEvidence.objects.count(), 1)

    def test_manual_merge_has_no_issue_completion_and_finishes(self):
        self.complete_merge()
        self.write("verified_delivery", self.final_evidence())
        self.terminate()
        result = finish_run(
            runner=self.runner,
            run_id=self.run.pk,
            claim_token=self.claim,
            status="succeeded",
            result_url=self.pull["url"],
            outcome="merged",
        )
        self.assertEqual(result.native_outcome, "merged")
        self.assertEqual(RepositoryMergeFence.objects.get().state, "completed")
        self.assertFalse(MergeEvidence.objects.exists())

    def test_completion_authority_survives_own_issue_close_and_pause(self):
        self.github_source()
        self.complete_merge()
        QueueItem.objects.filter(pk=self.item.pk).update(issue_state="closed")
        saved = RunnerSettings.objects.get(pk=1)
        saved.paused = True
        saved.save()
        self.write("native_intent", self.merge_intent("comment"))
        grant = secret_document(
            {
                "scope": "completion",
                "run_id": str(self.run.pk),
                "claim_token": str(self.claim),
                "purpose": "repository",
            },
            self.runner,
        )
        self.assertEqual(grant["access_mode"], "completion-reporting-only")

    def test_completion_scope_requires_actual_merge(self):
        self.start_merge()
        with self.assertRaisesMessage(RunConflict, "Provider-verified merge"):
            secret_document(
                {
                    "scope": "completion",
                    "run_id": str(self.run.pk),
                    "claim_token": str(self.claim),
                    "purpose": "repository",
                },
                self.runner,
            )

    def test_completion_cannot_target_unrelated_issue(self):
        self.github_source()
        self.complete_reporting()
        value = dict(self.completion, issue_number=2)
        with self.assertRaisesMessage(RunConflict, "frozen source issue"):
            self.write("native_completion", value)

    def test_cancel_racing_actual_merge_preserves_success(self):
        self.github_source()
        self.complete_reporting()
        self.run.cancellation_requested_at = timezone.now()
        self.run.save(update_fields=["cancellation_requested_at"])
        self.write("verified_delivery", self.final_evidence())
        self.terminate()
        result = finish_run(
            runner=self.runner,
            run_id=self.run.pk,
            claim_token=self.claim,
            status="succeeded",
            result_url=self.pull["url"],
            outcome="merged",
        )
        self.assertEqual(result.native_outcome, "merged")
        self.assertTrue(MergeEvidence.objects.exists())

    def test_provider_read_rejects_closed_unmerged_pr(self):
        self.complete_merge()
        client = Mock()
        client.repository.return_value = (
            {"repository": self.workspace["repository"]},
            {},
        )
        raw = {
            "state": "closed",
            "number": 9,
            "merged": False,
            "html_url": self.pull["url"],
            "head": {
                "repo": {"id": 123, "full_name": self.workspace["repository"]},
                "ref": self.workspace["branch"],
                "sha": self.workspace["commit_sha"],
            },
            "base": {
                "repo": {"id": 123, "full_name": self.workspace["repository"]},
                "ref": "main",
            },
        }
        client.pull.return_value = raw
        with patch("runner_control.merge_services.provider", return_value=client):
            with self.assertRaisesMessage(RunConflict, "does not confirm"):
                provider_merged(self.run, self.merged)

    def test_provider_read_failure_cannot_record_merge(self):
        self.start_merge()
        client = Mock()
        client.repository.side_effect = DeliveryError("Provider unavailable")
        value = {"workspace": self.workspace, "pull_request": self.pull, "merge": {}}
        with patch("runner_control.merge_services.provider", return_value=client):
            with self.assertRaisesMessage(RunConflict, "Provider unavailable"):
                provider_merged(self.run, value)
        self.assertFalse(MergeEvidence.objects.exists())

    def test_provider_completion_rejects_comment_on_other_issue(self):
        self.github_source()
        self.complete_reporting()
        client = Mock()
        client.request.side_effect = [
            {"id": 101, "number": 1, "state": "closed"},
            {
                "id": 77,
                "issue_url": "https://api.github.com/repos/example/configured/issues/2",
                "body": self.completion["marker"],
                "user": {"id": 456},
            },
        ]
        with patch("runner_control.merge_services.provider", return_value=client):
            with self.assertRaisesMessage(RunConflict, "does not belong"):
                provider_completed(self.run, self.completion, self.merged)

    def test_new_base_without_revision_is_rejected(self):
        self.complete_delivery()
        changed = dict(self.workspace, base_revision="b" * 40)
        with self.assertRaisesMessage(RunConflict, "immutable publication revision"):
            check_workspace(self.run, changed, self.context)

    def test_completion_exception_does_not_allow_new_execution(self):
        self.github_source()
        self.complete_merge()
        self.run.item.refresh_from_db()
        self.run.item.issue_state = "closed"
        with self.assertRaisesMessage(RunConflict, "Issue is closed"):
            authorize(self.run)


class MergeFenceConcurrencyTests(TransactionTestCase):
    def test_only_one_concurrent_repository_merge_reserves_fence(self):
        if connection.vendor != "postgresql":
            self.skipTest("Repository fence concurrency requires PostgreSQL.")
        runner = RunnerIdentity.objects.create(
            name="merge-fence", token_digest="a" * 64
        )
        runs = [Run.objects.create(runner=runner, state="empty") for _ in range(2)]
        barrier = Barrier(2)

        def reserve(run):
            connections.close_all()
            barrier.wait(timeout=5)
            try:
                reserve_fence(
                    run,
                    {
                        "publication_id": str(uuid4()),
                        "workspace": {
                            "repository_id": 123,
                            "commit_sha": "b" * 40,
                            "base_revision": "c" * 40,
                        },
                    },
                    uuid4(),
                )
                return "reserved"
            except RunConflict:
                return "conflict"
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(reserve, runs))
        self.assertCountEqual(outcomes, ["reserved", "conflict"])
        self.assertEqual(RepositoryMergeFence.objects.count(), 1)
