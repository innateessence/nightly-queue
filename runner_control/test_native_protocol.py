import copy
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from unittest.mock import patch
from uuid import UUID, uuid4, uuid5

from django.core.exceptions import ValidationError
from django.db import connection, connections
from django.test import Client, TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from credentials.models import RunnerPreflightGrant
from credentials.test_models import TEST_KEY
from projects.models import Project
from vibearr_executor.artifacts import pack, result_manifest
from vibearr_executor.contracts import canonical, digest
from vibearr_executor.validation_profile import CHECK_IDS, ValidationProfile
from vibearr_runner import native_contract as contract
from vibearr_runner.native_codex import CODEX_VERSION, codex_profile
from vibearr_runner.native_sessions import NativeSession
from work_queue.models import QueueItem
from work_queue.services import enqueue_item, requeue_items, refresh_issue_for_launch

from .auth import token_digest
from .configuration import configuration_document
from .models import RunnerIdentity
from .native_services import candidates, selected_account, update_status
from .plan_services import save_context, save_plan
from .recovery_services import request_cancellation
from .retention import acknowledge, purge_run
from .secrets import secret_document
from .services import RunConflict, claim_run, finish_run, record_evidence
from .settings_models import RunnerSettings
from .test_settings import ConfiguredProjectMixin
from .test_support import CONTROLLER_SECRET, provision_controller


class NativeProtocolMixin(ConfiguredProjectMixin):
    def setUp(self):
        super().setUp()
        for selected, value in (
            (self.github, "github-native-secret"),
            (self.backend, "backend-native-secret"),
        ):
            selected.set_secret(value)
            type(selected).objects.filter(pk=selected.pk).update(
                encrypted_payload=selected.encrypted_payload
            )
        saved = RunnerSettings.objects.get(pk=1)
        saved.enabled = True
        saved.validation_commands = []
        saved.execution_policy.update(
            runtime_profile="codex-token-v1", validation_profile="vibearr-python312-v1"
        )
        saved.save()
        Project.objects.filter(pk=self.project.pk).update(
            github_repository_id=123, github_owner_id=456, github_default_branch="main"
        )
        self.project.refresh_from_db()
        self.runner_token = "native-protocol-runner"
        self.runner = RunnerIdentity.objects.create(
            name="native", token_digest=token_digest(self.runner_token)
        )
        self.client.defaults["HTTP_AUTHORIZATION"] = "Bearer " + self.runner_token
        provision_controller(self)
        self.item = enqueue_item(
            project=self.project,
            title="Bounded native task",
            instructions="Implement useful work",
        )
        self.run = claim_run(
            runner=self.runner,
            run_id=uuid4(),
            configuration_revision=configuration_document()["revision"],
        )
        self.item.refresh_from_db()
        self.claim = self.item.claim_token
        self.original = pack(
            {
                "requirements.txt": (0o644, b"example==1.0\n"),
                "file.py": (0o644, b"before\n"),
            }
        )
        self.changed = pack(
            {
                "requirements.txt": (0o644, b"example==1.0\n"),
                "file.py": (0o644, b"after\n"),
            }
        )
        self.profile = ValidationProfile(
            "sha256:" + "a" * 64, "sha256:" + "b" * 64, digest(b"example==1.0\n")
        )
        self.deadline = (timezone.now() + timedelta(seconds=120)).isoformat()
        self.operations = []
        self.intent_ids = []
        self.publications = []

    def write(self, kind, payload, operation_id=None):
        return record_evidence(
            runner=self.runner,
            run_id=self.run.pk,
            claim_token=self.claim,
            operation_id=operation_id
            or (
                UUID(payload["operation_id"])
                if kind == "verified_delivery"
                else uuid4()
            ),
            kind=kind,
            payload=payload,
        )

    def post(self, name, data):
        return self.client.post(
            reverse(name, args=[self.run.pk]),
            json.dumps(data),
            content_type="application/json",
        )

    def prepare_context(self):
        observation = {
            "repository_id": 123,
            "owner_id": 456,
            "repository_url": self.project.repository_url,
            "default_branch": "main",
        }
        with patch("work_queue.github.repository_metadata", return_value=observation):
            refresh_issue_for_launch(
                item_id=self.item.pk,
                claim_token=self.claim,
                token="selected",
                runner=self.runner,
                run_id=self.run.pk,
                mode="initial",
            )
        self.run.refresh_from_db()
        self.item.refresh_from_db()
        document = contract.context_document(
            run_id=str(self.run.pk),
            source=self.run.source_snapshot,
            repository={
                "repository_id": 123,
                "repository": "example/configured",
                "repository_url": self.project.repository_url,
                "base_branch": "main",
                "base_revision": "c" * 40,
                "base_tree_sha": "d" * 40,
                "branch": self.item.branch_name,
            },
            source_digest=digest(self.original),
            policy_digest=self.run.configuration_snapshot["policy_digest"],
            account=selected_account(self.run),
            validation_profile=self.profile,
            runtime_binding_digest="e" * 64,
            acceptance_criteria=["Pass the independent required validation checks."],
        )
        self.context = save_context(
            runner=self.runner,
            run_id=self.run.pk,
            claim_token=self.claim,
            operation_id=uuid4(),
            document=document,
            base_commit="c" * 40,
            resolved_base_branch="main",
            reason="",
        )
        plan = contract.plan_document(
            context_id=str(self.context.pk),
            context_digest=self.context.digest,
            problem="Implement the frozen task",
            approach="Change code and validate it",
            acceptance_criteria=document["acceptance_criteria"],
        )
        save_plan(
            runner=self.runner,
            run_id=self.run.pk,
            claim_token=self.claim,
            operation_id=uuid4(),
            document=plan,
            reason="",
        )

    def common(self):
        return {
            "context_id": str(self.context.pk),
            "context_digest": self.context.digest,
            "policy_digest": self.run.configuration_snapshot["policy_digest"],
            "attempt": 0,
        }

    def prepare_agent(self):
        self.prepare_context()
        fixed = codex_profile("sha256:" + "f" * 64, "token")
        manifest = {
            "schema_version": 1,
            "run_id": str(self.run.pk),
            "operation_id": str(uuid4()),
            "profile_id": fixed.profile_id,
            "profile_digest": fixed.digest,
            "source_digest": digest(self.original),
            "context_digest": "1" * 64,
            "phase": "agent",
            "limits": fixed.limits.__dict__,
            "deadline": self.deadline,
            "network_hosts": list(fixed.network_hosts),
        }
        self.session = NativeSession(
            "codex",
            CODEX_VERSION,
            selected_account(self.run),
            "token",
            fixed.image,
            "e" * 64,
            manifest,
        )
        self.agent_intent = self.write(
            "native_intent",
            contract.agent_intent(**self.common(), session=self.session),
        )
        self.intent_ids.append(str(self.agent_intent.operation_id))

    def observed(self, operation_id, original, changed, exit_code=0):
        end = timezone.now().timestamp()
        return {
            "state": "exited",
            "operation_id": operation_id,
            "descendants_terminated": True,
            "container_id": "2" * 64,
            "result": {
                "schema_version": 1,
                "run_id": str(self.run.pk),
                "operation_id": operation_id,
                "terminated": True,
                "exit_code": exit_code,
                "timed_out": False,
                "canceled": False,
                "log_truncated": False,
                "started_at": end - 1,
                "ended_at": end,
                "log_digest": "3" * 64,
                "artifact": result_manifest(original, changed),
            },
        }

    def complete_agent(self):
        self.prepare_agent()
        observed = self.observed(
            self.session.manifest["operation_id"], self.original, self.changed
        )
        self.operations.append(observed)
        outcome = {
            "result": observed["result"],
            "artifact": observed["result"]["artifact"],
            "source": self.changed,
            "successful": True,
            "session_id": "ses_provider-identity",
            "agent_version": CODEX_VERSION,
        }
        self.write(
            "native_agent",
            contract.agent_result(
                intent_id=str(self.agent_intent.operation_id),
                session=self.session,
                outcome=outcome,
                observed=observed,
            ),
        )
        self.workspace = {
            "repository_id": 123,
            "repository": "example/configured",
            "branch": self.item.branch_name,
            "base_branch": "main",
            "base_revision": "c" * 40,
            "tree_sha": "4" * 40,
            "commit_sha": "5" * 40,
        }
        self.workspace_entry = self.write(
            "native_workspace",
            contract.workspace_record(
                context_id=str(self.context.pk),
                context_digest=self.context.digest,
                attempt=0,
                agent_intent_id=str(self.agent_intent.operation_id),
                workspace=self.workspace,
                source_digest=digest(self.changed),
                export_digest=digest(self.changed),
                requirements_digest=self.profile.dependency_digest,
            ),
        )

    def complete_validation(self):
        self.complete_agent()
        attempt_id = str(uuid4())
        context_hash = digest(
            canonical(
                {
                    "schema_version": 1,
                    "kind": "validation",
                    "tree_sha": self.workspace["tree_sha"],
                    "source_digest": digest(self.changed),
                    "validation_profile_digest": self.profile.digest,
                }
            )
        )
        operations = []
        for name, fixed in zip(CHECK_IDS, self.profile.executor_profiles()):
            limits = {
                key: min(value, self.session.manifest["limits"][key])
                for key, value in fixed.limits.__dict__.items()
            }
            manifest = {
                "schema_version": 1,
                "run_id": str(self.run.pk),
                "operation_id": str(uuid5(UUID(attempt_id), name)),
                "profile_id": fixed.profile_id,
                "profile_digest": fixed.digest,
                "source_digest": digest(self.changed),
                "context_digest": context_hash,
                "phase": "validation",
                "limits": limits,
                "deadline": self.deadline,
                "network_hosts": [],
            }
            operations.append({"id": name, "manifest": manifest})
        session = {
            "schema_version": 1,
            "run_id": str(self.run.pk),
            "attempt_id": attempt_id,
            "profile": self.profile.data(),
            "profile_digest": self.profile.digest,
            "tree_sha": self.workspace["tree_sha"],
            "source_digest": digest(self.changed),
            "operations": operations,
        }
        self.validation_intent = self.write(
            "native_intent",
            contract.validation_intent(
                **self.common(),
                session=session,
                workspace_id=str(self.workspace_entry.operation_id),
            ),
        )
        self.intent_ids.append(str(self.validation_intent.operation_id))
        observed = [
            self.observed(entry["manifest"]["operation_id"], self.changed, self.changed)
            for entry in operations
        ]
        self.operations.extend(observed)
        value = {
            "schema_version": 1,
            "intent_id": str(self.validation_intent.operation_id),
            "run_id": str(self.run.pk),
            "tree_sha": self.workspace["tree_sha"],
            "source_digest": digest(self.changed),
            "profile": contract.profile_reference(self.profile),
            "checks": [
                {"id": entry["id"], "receipt": contract.receipt_summary(status)}
                for entry, status in zip(operations, observed)
            ],
            "state": "passed",
            "provenance": None,
        }
        self.validation = self.write(
            "native_validation",
            contract.validation_result(
                intent_id=str(self.validation_intent.operation_id),
                session=session,
                operations=observed,
                result={
                    "state": "passed",
                    "validation": contract.validation_evidence(value),
                },
            ),
        )

    def complete_delivery(self):
        self.complete_validation()
        publication_id = str(uuid4())
        push = self.write(
            "native_intent",
            contract.delivery_intent(
                **self.common(),
                publication_id=publication_id,
                stage="push",
                workspace_id=str(self.workspace_entry.operation_id),
                workspace=self.workspace,
            ),
        )
        self.intent_ids.append(str(push.operation_id))
        self.write(
            "native_delivery",
            contract.delivery_record(
                intent_ids=[str(push.operation_id)],
                workspace_id=str(self.workspace_entry.operation_id),
                publication_id=publication_id,
                workspace=self.workspace,
                pushed_sha=self.workspace["commit_sha"],
            ),
        )
        pr = self.write(
            "native_intent",
            contract.delivery_intent(
                **self.common(),
                publication_id=publication_id,
                stage="create_pr",
                workspace_id=str(self.workspace_entry.operation_id),
                workspace=self.workspace,
            ),
        )
        self.intent_ids.append(str(pr.operation_id))
        self.pull = {
            "repository_id": 123,
            "number": 9,
            "url": "https://github.com/example/configured/pull/9",
            "base": "main",
            "head": self.item.branch_name,
            "head_sha": self.workspace["commit_sha"],
        }
        self.write(
            "native_delivery",
            contract.delivery_record(
                intent_ids=[str(push.operation_id), str(pr.operation_id)],
                workspace_id=str(self.workspace_entry.operation_id),
                publication_id=publication_id,
                workspace=self.workspace,
                pushed_sha=self.workspace["commit_sha"],
                pull_request=self.pull,
            ),
        )
        for intent, stage in ((push, "push"), (pr, "create_pr")):
            self.publications.append(
                {
                    "intent_id": str(intent.operation_id),
                    "publication_id": publication_id,
                    "stage": stage,
                    "state": "known_pr",
                    "pushed_sha": self.workspace["commit_sha"],
                    "pull_request": self.pull,
                }
            )
        self.evidence = {
            "schema_version": 2,
            "execution_kind": "native",
            "run_id": str(self.run.pk),
            "operation_id": str(uuid4()),
            "runner_id": self.runner.pk,
            "configuration_revision": self.run.configuration_revision,
            "policy_digest": self.run.configuration_snapshot["policy_digest"],
            "context": {"id": str(self.context.pk), "digest": self.context.digest},
            "runtime": {
                "image_digest": self.session.image_digest,
                "profile_id": self.session.manifest["profile_id"],
                "agent_version": CODEX_VERSION,
                "session_id": "ses_provider-identity",
            },
            "workspace": self.workspace,
            "validation": contract.validation_evidence(self.validation.payload),
            "delivery": {
                "mode": "pull_request",
                "pushed_sha": self.workspace["commit_sha"],
                "pull_request": self.pull,
                "checks": [],
                "merge": None,
                "issue_completion": None,
            },
        }

    def terminate(self):
        return self.write(
            "native_termination",
            contract.termination_record(
                intent_ids=self.intent_ids,
                operations=self.operations,
                local_processes={},
                publications=self.publications,
            ),
        )


@override_settings(NQ_CREDENTIAL_KEY=TEST_KEY)
class NativeProtocolTests(NativeProtocolMixin, TestCase):
    def test_native_context_and_plan_need_no_approval_or_research(self):
        self.prepare_agent()
        self.assertFalse(self.run.approvals.exists())
        self.assertEqual(
            self.run.contexts.get().document["source"], self.run.source_snapshot
        )
        self.assertEqual(self.run.evidence.get().kind, "native_intent")

    def test_complete_receipt_chain_authorizes_only_exact_pr(self):
        self.complete_delivery()
        self.write("verified_delivery", self.evidence)
        self.terminate()
        result = finish_run(
            runner=self.runner,
            run_id=self.run.pk,
            claim_token=self.claim,
            status="succeeded",
            result_url=self.pull["url"],
            outcome="delivered",
        )
        self.assertEqual(result.native_outcome, "delivered")
        self.assertEqual(result.state, "completed")

    def test_finish_keeps_ownership_until_all_resources_terminal(self):
        self.complete_agent()
        with self.assertRaisesMessage(RunConflict, "Confirm every"):
            finish_run(
                runner=self.runner,
                run_id=self.run.pk,
                claim_token=self.claim,
                status="failed",
                error_summary="Stopped",
            )
        self.run.refresh_from_db()
        self.assertEqual(self.run.active_slot, 1)

    def test_retry_after_intent_requests_cancel_and_retains_claim(self):
        self.prepare_agent()
        result = requeue_items(
            QueueItem.objects.filter(pk=self.item.pk), return_summary=True
        )
        self.run.refresh_from_db()
        self.item.refresh_from_db()
        self.assertEqual(result, {"requeued": 0, "pending": 1, "held": 0})
        self.assertEqual(self.item.claim_token, self.claim)
        self.assertEqual(self.run.active_slot, 1)
        self.assertIsNotNone(self.run.retry_requested_at)

    def test_retry_without_dispatch_remains_immediate(self):
        self.assertEqual(requeue_items(QueueItem.objects.filter(pk=self.item.pk)), 1)
        self.run.refresh_from_db()
        self.assertEqual(self.run.state, "obsolete")

    def test_terminal_fact_after_cancel_allows_pending_retry_completion(self):
        self.complete_agent()
        requeue_items(QueueItem.objects.filter(pk=self.item.pk))
        self.terminate()
        response = self.post(
            "runner_cancellation",
            {"claim_token": str(self.claim), "termination_confirmed": True},
        )
        self.assertEqual(response.status_code, 200, response.content)
        finish_run(
            runner=self.runner,
            run_id=self.run.pk,
            claim_token=self.claim,
            status="failed",
            error_summary="Canceled",
            outcome="canceled",
        )
        self.item.refresh_from_db()
        self.assertEqual(self.item.status, "queued")
        self.assertIsNone(self.item.claim_token)

    def test_known_delivery_remains_held_after_cancel_retry(self):
        self.complete_delivery()
        requeue_items(QueueItem.objects.filter(pk=self.item.pk))
        self.terminate()
        self.assertEqual(
            self.post(
                "runner_cancellation",
                {"claim_token": str(self.claim), "termination_confirmed": True},
            ).status_code,
            200,
        )
        finish_run(
            runner=self.runner,
            run_id=self.run.pk,
            claim_token=self.claim,
            status="failed",
            error_summary="Canceled after publication",
            outcome="canceled",
        )
        self.item.refresh_from_db()
        self.assertEqual(self.item.status, "failed")
        self.assertEqual(self.item.result_url, self.pull["url"])
        self.assertEqual(
            requeue_items(
                QueueItem.objects.filter(pk=self.item.pk), return_summary=True
            )["held"],
            1,
        )

    def test_unknown_native_operation_cannot_confirm_cancellation(self):
        self.prepare_agent()
        request_cancellation(self.run.pk)
        response = self.post(
            "runner_cancellation",
            {"claim_token": str(self.claim), "termination_confirmed": True},
        )
        self.assertEqual(response.status_code, 409)
        self.run.refresh_from_db()
        self.assertIsNone(self.run.cancellation_confirmed_at)

    def test_new_intent_is_rejected_after_cancel(self):
        self.complete_agent()
        request_cancellation(self.run.pk)
        with self.assertRaises(RunConflict):
            self.write("native_intent", self.agent_intent.payload)

    def test_delivery_receipt_rejects_missing_required_check(self):
        self.complete_delivery()
        self.evidence["validation"]["checks"].pop()
        with self.assertRaises((ValidationError, RunConflict)):
            self.write("verified_delivery", self.evidence)

    def test_delivery_receipt_rejects_other_tree(self):
        self.complete_delivery()
        self.evidence["workspace"]["tree_sha"] = "9" * 40
        with self.assertRaises((ValidationError, RunConflict)):
            self.write("verified_delivery", self.evidence)

    def test_delivery_receipt_rejects_model_claimed_session(self):
        self.complete_delivery()
        self.evidence["runtime"]["session_id"] = "ses_forged"
        with self.assertRaisesMessage(RunConflict, "protected session"):
            self.write("verified_delivery", self.evidence)

    def test_delivery_receipt_rejects_other_profile(self):
        self.complete_delivery()
        self.evidence["validation"]["profile_digest"] = "9" * 64
        with self.assertRaises((ValidationError, RunConflict)):
            self.write("verified_delivery", self.evidence)

    def test_status_is_monotonic_and_does_not_spend_evidence_budget(self):
        fields = dict(runner=self.runner, run_id=self.run.pk, claim_token=self.claim)
        update_status(
            **fields,
            data={"sequence": 2, "stage": "implementation", "session_id": "ses_active"},
        )
        update_status(**fields, data={"sequence": 1, "stage": "waiting"})
        self.run.refresh_from_db()
        self.assertEqual(self.run.native_status["sequence"], 2)
        self.assertFalse(self.run.evidence.exists())
        with self.assertRaisesMessage(RunConflict, "different content"):
            update_status(**fields, data={"sequence": 2, "stage": "waiting"})

    def test_github_preflight_requires_grant_and_selected_project(self):
        fields = {
            "scope": "preflight",
            "backend": "github",
            "credential_id": self.github.pk,
            "project_id": self.project.pk,
        }
        with self.assertRaises(ValidationError):
            secret_document(fields, self.runner)
        RunnerPreflightGrant.objects.create(runner=self.runner, credential=self.github)
        self.assertEqual(
            secret_document(fields, self.runner)["access_mode"], "preflight-read-only"
        )
        fields["project_id"] = 99999
        with self.assertRaises(ValidationError):
            secret_document(fields, self.runner)

    def test_reconciliation_can_read_after_cancel_but_cannot_get_model_key(self):
        self.prepare_agent()
        request_cancellation(self.run.pk)
        fields = {
            "scope": "reconciliation",
            "run_id": str(self.run.pk),
            "claim_token": str(self.claim),
            "purpose": "repository",
        }
        self.assertEqual(
            secret_document(fields, self.runner)["access_mode"],
            "reconciliation-read-only",
        )
        fields["purpose"] = "implementation"
        with self.assertRaises(ValidationError):
            secret_document(fields, self.runner)

    def test_retention_tombstones_source_but_keeps_terminal_receipt_metadata(self):
        self.complete_agent()
        self.terminate()
        completed = finish_run(
            runner=self.runner,
            run_id=self.run.pk,
            claim_token=self.claim,
            status="failed",
            error_summary="No delivery",
        )
        acknowledge(self.runner, self.run.pk, self.claim, completed.finish_digest)
        purge_run(self.run.pk, now=timezone.now() + timedelta(days=366))
        self.run.refresh_from_db()
        self.assertTrue(self.run.source_snapshot["purged"])
        self.assertFalse(self.run.contexts.exists())
        self.assertTrue(self.run.evidence.filter(kind="native_intent").exists())
        self.assertTrue(self.run.evidence.filter(kind="native_termination").exists())

    def test_candidates_avoid_paid_idle_probes(self):
        other = Project.objects.create(
            name="Empty", repository_url="https://github.com/example/empty"
        )
        self.assertEqual(candidates(self.runner)["project_ids"], [])
        enqueue_item(project=other, title="Available work")
        self.assertEqual(candidates(self.runner)["project_ids"], [other.pk])
        RunnerSettings.objects.filter(pk=1).update(paused=True)
        self.assertEqual(candidates(self.runner)["project_ids"], [])

    def test_continuation_is_not_an_initial_freshness_bypass(self):
        with self.assertRaisesMessage(ValidationError, "registered native intent"):
            refresh_issue_for_launch(
                item_id=self.item.pk,
                claim_token=self.claim,
                token="selected",
                runner=self.runner,
                run_id=self.run.pk,
                mode="continuation",
            )

    def test_stable_repository_id_allows_observed_rename_at_publication(self):
        self.complete_validation()
        Project.objects.filter(pk=self.project.pk).update(
            repository_url="https://github.com/new-owner/renamed.git"
        )
        self.project.refresh_from_db()
        renamed = dict(self.workspace, repository="new-owner/renamed")
        with self.assertRaisesMessage(RunConflict, "current canonical"):
            self.write(
                "native_intent",
                contract.delivery_intent(
                    **self.common(),
                    publication_id=str(uuid4()),
                    stage="push",
                    workspace_id=str(self.workspace_entry.operation_id),
                    workspace=self.workspace,
                ),
            )
        self.write(
            "native_intent",
            contract.delivery_intent(
                **self.common(),
                publication_id=str(uuid4()),
                stage="push",
                workspace_id=str(self.workspace_entry.operation_id),
                workspace=renamed,
            ),
        )

    def test_missing_publication_result_cannot_confirm_termination(self):
        self.complete_delivery()
        self.publications.pop()
        with self.assertRaisesMessage(RunConflict, "every publication"):
            self.terminate()

    def test_changed_credential_version_cannot_dispatch(self):
        self.prepare_context()
        type(self.backend).objects.filter(pk=self.backend.pk).update(secret_version=2)
        with self.assertRaisesMessage(RunConflict, "credential changed"):
            from .native_services import authorize

            authorize(self.run)

    def test_project_filter_is_narrow_and_empty_replay_stays_empty(self):
        requeue_items(QueueItem.objects.filter(pk=self.item.pk))
        run_id = uuid4()
        empty = claim_run(
            runner=self.runner,
            run_id=run_id,
            configuration_revision=configuration_document()["revision"],
            project_ids=[],
        )
        self.assertEqual(empty.state, "empty")
        replay = claim_run(
            runner=self.runner, run_id=run_id, project_ids=[self.project.pk]
        )
        self.assertEqual(replay.state, "empty")
        self.item.refresh_from_db()
        self.assertEqual(self.item.status, "queued")

    def test_project_filter_rejects_boolean_id(self):
        requeue_items(QueueItem.objects.filter(pk=self.item.pk))
        with self.assertRaises(ValidationError):
            claim_run(runner=self.runner, run_id=uuid4(), project_ids=[True])

    def github_source(self):
        from work_queue.services import import_issue
        from work_queue.test_selection import source_issue

        self.project.autonomous = True
        self.project.enabled_at = timezone.now() - timedelta(days=1)
        self.project.save()
        self.item.source = "github"
        self.item.issue_number = 1
        self.item.github_repository_id = 123
        self.item.branch_name = "vibearr/issue-1"
        self.item.issue_repository = self.project.repository_url
        self.item.save()
        fresh = source_issue(
            title=self.item.title, body=self.item.instructions, author_id=456
        )
        import_issue(project=self.project, repository_id=123, **fresh)
        self.item.refresh_from_db()
        snapshot = {
            "title": self.item.title,
            "body": self.item.instructions,
            "repository_id": 123,
            "issue_number": 1,
            "revision": fresh["revision"],
            "metadata": self.item.source_metadata,
        }
        self.item.source_snapshot = snapshot
        self.item.save(update_fields=["source_snapshot"])
        self.run.source_snapshot = snapshot
        self.run.save(update_fields=["source_snapshot"])
        return fresh

    def test_continuation_keeps_frozen_discussion_after_status_comment(self):
        fresh = self.github_source()
        comments = [{"id": 1, "body": "Frozen instructions"}]
        with (
            patch("work_queue.github.read_issue", return_value=fresh),
            patch("work_queue.github.issue_comments", return_value=comments),
        ):
            self.prepare_agent()
        frozen = copy.deepcopy(self.run.source_snapshot)
        changed = dict(fresh, revision="8" * 64, updated_at=timezone.now().isoformat())
        observation = {
            "repository_id": 123,
            "owner_id": 456,
            "repository_url": self.project.repository_url,
            "default_branch": "main",
        }
        with (
            patch("work_queue.github.repository_metadata", return_value=observation),
            patch("work_queue.github.read_issue", return_value=changed),
            patch("work_queue.github.issue_comments") as read_comments,
        ):
            snapshot = refresh_issue_for_launch(
                item_id=self.item.pk,
                claim_token=self.claim,
                token="selected",
                runner=self.runner,
                run_id=self.run.pk,
                mode="continuation",
            )
        self.assertEqual(snapshot, frozen)
        self.run.refresh_from_db()
        self.assertEqual(self.run.source_snapshot, frozen)
        read_comments.assert_not_called()

    def test_continuation_rejects_material_instructions(self):
        fresh = self.github_source()
        with (
            patch("work_queue.github.read_issue", return_value=fresh),
            patch("work_queue.github.issue_comments", return_value=[]),
        ):
            self.prepare_agent()
        changed = dict(
            fresh,
            body="Different instructions",
            revision="8" * 64,
            updated_at=timezone.now().isoformat(),
        )
        observation = {
            "repository_id": 123,
            "owner_id": 456,
            "repository_url": self.project.repository_url,
            "default_branch": "main",
        }
        with (
            patch("work_queue.github.repository_metadata", return_value=observation),
            patch("work_queue.github.read_issue", return_value=changed),
        ):
            with self.assertRaisesMessage(ValidationError, "instructions changed"):
                refresh_issue_for_launch(
                    item_id=self.item.pk,
                    claim_token=self.claim,
                    token="selected",
                    runner=self.runner,
                    run_id=self.run.pk,
                    mode="continuation",
                )
        self.run.refresh_from_db()
        self.assertEqual(self.run.source_snapshot["body"], fresh["body"])

    def test_derived_validation_requires_owned_build_source_and_runtime(self):
        from vibearr_executor.qualification import REQUIRED_CHECKS
        from vibearr_executor.validation_profile import PACKAGE_HOSTS
        from .native_services import validation_profile_binding

        self.complete_agent()
        build_id = str(uuid4())
        fixed = self.profile.setup_profile()
        limits = {
            key: min(value, self.session.manifest["limits"][key])
            for key, value in fixed.limits.__dict__.items()
        }
        request = {
            "schema_version": 1,
            "run_id": str(self.run.pk),
            "build_id": build_id,
            "source_digest": digest(self.changed),
            "setup_profile_id": fixed.profile_id,
            "validation_profile": self.profile.data(),
            "limits": limits,
            "deadline": self.deadline,
            "network_hosts": list(PACKAGE_HOSTS),
        }
        self.write(
            "native_intent",
            contract.dependency_intent(
                **self.common(),
                request=request,
                workspace_id=str(self.workspace_entry.operation_id),
            ),
        )
        derived = ValidationProfile(
            "sha256:" + "9" * 64,
            self.profile.database_image,
            self.profile.dependency_digest,
        )
        proof = {
            "schema_version": 1,
            "build_id": build_id,
            "run_id": str(self.run.pk),
            "source_digest": digest(self.changed),
            "requirements_digest": self.profile.dependency_digest,
            "dependency_digest": "8" * 64,
            "parent_image": self.profile.image,
            "parent_profile_digest": self.profile.digest,
            "parent_runtime_binding": "e" * 64,
            "setup_operation_id": build_id,
            "setup_log_digest": "7" * 64,
            "derived_image": derived.image,
            "qualification_binding_digest": "6" * 64,
            "qualification_report_digest": "5" * 64,
            "boundary_checks": list(REQUIRED_CHECKS),
        }
        value = {"profile": contract.profile_reference(derived), "provenance": proof}
        self.assertEqual(
            validation_profile_binding(
                self.run, value, self.workspace_entry.payload, self.context
            ),
            build_id,
        )
        proof["source_digest"] = "a" * 64
        with self.assertRaisesMessage(RunConflict, "owned source/runtime"):
            validation_profile_binding(
                self.run, value, self.workspace_entry.payload, self.context
            )

    def test_retention_preserves_newer_attempt_source(self):
        self.complete_agent()
        self.terminate()
        completed = finish_run(
            runner=self.runner,
            run_id=self.run.pk,
            claim_token=self.claim,
            status="failed",
            error_summary="Retry later",
        )
        acknowledge(self.runner, self.run.pk, self.claim, completed.finish_digest)
        requeue_items(QueueItem.objects.filter(pk=self.item.pk))
        QueueItem.objects.filter(pk=self.item.pk).update(
            instructions="New explicit attempt"
        )
        newer = claim_run(
            runner=self.runner,
            run_id=uuid4(),
            configuration_revision=configuration_document()["revision"],
        )
        frozen = copy.deepcopy(newer.source_snapshot)
        purge_run(self.run.pk, now=timezone.now() + timedelta(days=366))
        newer.refresh_from_db()
        self.item.refresh_from_db()
        self.assertEqual(newer.source_snapshot, frozen)
        self.assertEqual(self.item.source_snapshot, frozen)
        self.assertEqual(frozen["body"], "New explicit attempt")


@override_settings(NQ_CREDENTIAL_KEY=TEST_KEY)
class NativeProtocolConcurrencyTests(NativeProtocolMixin, TransactionTestCase):
    def parallel(self, first, second):
        barrier = Barrier(2)

        def call(operation):
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SET statement_timeout = '5s'")
                barrier.wait(timeout=5)
                return operation()
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(call, operation) for operation in (first, second)]
            return [future.result(timeout=10) for future in futures]

    def upload(self, payload, operation_id):
        client = Client(
            HTTP_AUTHORIZATION="Bearer " + self.runner_token,
            HTTP_X_NQ_CONTROLLER_SECRET=CONTROLLER_SECRET,
        )
        response = client.post(
            reverse("runner_artifact", args=[self.run.pk]),
            json.dumps(
                {
                    "claim_token": str(self.claim),
                    "operation_id": str(operation_id),
                    "kind": "native_intent",
                    "data": payload,
                }
            ),
            content_type="application/json",
        )
        return response.status_code

    def test_concurrent_duplicate_native_intent_is_single_dispatch_identity(self):
        self.prepare_agent()
        payload = self.agent_intent.payload
        identity = self.agent_intent.operation_id
        responses = self.parallel(
            lambda: self.upload(payload, identity),
            lambda: self.upload(payload, identity),
        )
        self.assertEqual(responses, [200, 200])
        self.assertEqual(self.run.evidence.filter(kind="native_intent").count(), 1)

    def test_retry_racing_intent_preserves_one_owner_or_fences_dispatch(self):
        self.prepare_agent()
        payload = self.agent_intent.payload
        identity = self.agent_intent.operation_id
        self.agent_intent.delete()
        responses = self.parallel(
            lambda: self.upload(payload, identity),
            lambda: requeue_items(QueueItem.objects.filter(pk=self.item.pk)),
        )
        self.run.refresh_from_db()
        self.item.refresh_from_db()
        if responses[0] == 200:
            self.assertEqual(self.run.state, "active")
            self.assertEqual(self.run.active_slot, 1)
            self.assertEqual(self.item.claim_token, self.claim)
            self.assertIsNotNone(self.run.retry_requested_at)
        else:
            self.assertEqual(responses[0], 409)
            self.assertEqual(self.run.state, "obsolete")
            self.assertFalse(self.run.evidence.filter(kind="native_intent").exists())

    def test_cancel_racing_intent_never_releases_claim(self):
        self.prepare_agent()
        payload = self.agent_intent.payload
        identity = self.agent_intent.operation_id
        self.agent_intent.delete()
        responses = self.parallel(
            lambda: self.upload(payload, identity),
            lambda: request_cancellation(self.run.pk),
        )
        self.assertIn(responses[0], [200, 409])
        self.run.refresh_from_db()
        self.item.refresh_from_db()
        self.assertEqual(self.run.active_slot, 1)
        self.assertEqual(self.item.claim_token, self.claim)
        self.assertIsNotNone(self.run.cancellation_requested_at)

    def test_changed_controller_proof_rejects_native_intent(self):
        self.prepare_agent()
        payload = self.agent_intent.payload
        self.agent_intent.delete()
        RunnerIdentity.objects.filter(pk=self.runner.pk).update(
            controller_digest=token_digest("other-proof")
        )
        self.assertEqual(self.upload(payload, uuid4()), 409)
        self.assertFalse(self.run.evidence.filter(kind="native_intent").exists())

    def test_controller_proof_change_serializes_with_dispatch(self):
        from django.db import transaction
        from .services import lock_controller

        self.prepare_agent()
        payload = self.agent_intent.payload
        self.agent_intent.delete()

        def rotate():
            with transaction.atomic():
                lock_controller()
                return RunnerIdentity.objects.filter(pk=self.runner.pk).update(
                    controller_digest=token_digest("new-volume-proof")
                )

        responses = self.parallel(lambda: self.upload(payload, uuid4()), rotate)
        self.assertIn(responses[0], [200, 409])
        self.run.refresh_from_db()
        self.assertEqual(self.run.active_slot, 1)
        self.assertEqual(
            self.run.evidence.filter(kind="native_intent").count(),
            int(responses[0] == 200),
        )


@override_settings(NQ_CREDENTIAL_KEY=TEST_KEY)
class NativeHTTPTests(NativeProtocolMixin, TestCase):
    def test_native_initial_source_http_observes_manual_repository(self):
        observation = {
            "repository_id": 123,
            "owner_id": 456,
            "repository_url": self.project.repository_url,
            "default_branch": "main",
        }
        with patch("work_queue.github.repository_metadata", return_value=observation):
            response = self.post(
                "runner_refresh_source",
                {"claim_token": str(self.claim), "mode": "initial"},
            )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(response.json()["eligible"])
        self.assertEqual(response.json()["repository"]["repository_id"], 123)

    def test_source_rejects_structured_mode(self):
        response = self.post(
            "runner_refresh_source", {"claim_token": str(self.claim), "mode": []}
        )
        self.assertEqual(response.status_code, 400)

    def test_status_http_updates_without_immutable_artifact(self):
        response = self.post(
            "runner_native_status",
            {
                "claim_token": str(self.claim),
                "sequence": 1,
                "stage": "implementation",
                "session_id": "ses_active",
            },
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(
            response.json()["run"]["native_status"]["session_id"], "ses_active"
        )
        self.assertFalse(self.run.evidence.exists())

    def test_source_checkpoint_enforces_current_pause(self):
        self.prepare_agent()
        RunnerSettings.objects.filter(pk=1).update(paused=True)
        observation = {
            "repository_id": 123,
            "owner_id": 456,
            "repository_url": self.project.repository_url,
            "default_branch": "main",
        }
        with patch("work_queue.github.repository_metadata", return_value=observation):
            response = self.post(
                "runner_refresh_source",
                {"claim_token": str(self.claim), "mode": "continuation"},
            )
        self.assertEqual(response.status_code, 409, response.content)

    def test_malformed_native_artifact_returns_validation_error(self):
        response = self.post(
            "runner_artifact",
            {
                "claim_token": str(self.claim),
                "operation_id": str(uuid4()),
                "kind": "native_intent",
                "data": {"schema_version": 1},
            },
        )
        self.assertEqual(response.status_code, 400)

    def test_stale_run_retry_cannot_cancel_newer_attempt(self):
        previous = self.run.pk
        requeue_items(QueueItem.objects.filter(pk=self.item.pk))
        newer = claim_run(
            runner=self.runner,
            run_id=uuid4(),
            configuration_revision=configuration_document()["revision"],
        )
        response = self.client.post(reverse("runner_operator_retry", args=[previous]))
        self.assertEqual(response.status_code, 409)
        newer.refresh_from_db()
        self.assertIsNone(newer.cancellation_requested_at)
        self.assertEqual(newer.active_slot, 1)

    def test_native_status_presentation_redacts_selected_material(self):
        from .plan_presentation import present_run

        self.run.native_status = {"detail": "backend-native-secret"}
        self.run.save(update_fields=["native_status"])
        self.assertEqual(present_run(self.run).native_status["detail"], "[redacted]")

    def test_unavailable_redaction_hides_native_status_as_empty_mapping(self):
        from .plan_presentation import present_run

        self.run.native_status = {"detail": "Untrusted historical status"}
        type(self.backend).objects.filter(pk=self.backend.pk).update(
            encrypted_payload="invalid-payload"
        )
        self.assertEqual(present_run(self.run).native_status, {})
