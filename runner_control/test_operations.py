import uuid
import json
from datetime import timedelta
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from cryptography.fernet import Fernet
from django.urls import reverse
from django.utils import timezone

from projects.models import Project
from work_queue.services import enqueue_item

from .auth import token_digest
from .models import (
    ContextSnapshot,
    PlanVersion,
    Run,
    RunEvidence,
    RunRetention,
    RunnerIdentity,
)
from .release import complete
from .retention import acknowledge, plan, purge_run, workspace_removed
from .services import RunConflict
from .test_support import provision_controller


class RetentionTests(TestCase):
    def setUp(self):
        self.runner = RunnerIdentity.objects.create(
            name="operations", token_digest=token_digest("test-token")
        )
        provision_controller(self)
        self.project = Project.objects.create(
            name="Test", repository_url="https://github.com/example/test"
        )
        self.item = enqueue_item(
            project=self.project,
            title="Test task",
            instructions="Keep recovery evidence",
        )
        self.token = uuid.uuid4()
        self.run = Run.objects.create(
            runner=self.runner,
            item=self.item,
            state="completed",
            stage="finished",
            finished_at=timezone.now() - timedelta(days=100),
            claim_digest=token_digest(str(self.token)),
            finish_digest="f" * 64,
        )
        self.retention = RunRetention.objects.create(run=self.run)

    def ack(self):
        return acknowledge(self.runner, self.run.pk, self.token, self.run.finish_digest)

    def documents(self):
        context = ContextSnapshot.objects.create(
            run=self.run,
            operation_id=uuid.uuid4(),
            document={"title": "saved"},
            digest="a" * 64,
            reason="Original",
            sequence=1,
            inspected_base_commit="a" * 40,
            resolved_base_branch="main",
            discussion_digest="b" * 64,
        )
        first = PlanVersion.objects.create(
            run=self.run,
            context=context,
            version=1,
            operation_id=uuid.uuid4(),
            document={},
            digest="c" * 64,
            reason="Original",
        )
        PlanVersion.objects.create(
            run=self.run,
            context=context,
            version=2,
            supersedes=first,
            operation_id=uuid.uuid4(),
            document={},
            digest="d" * 64,
            reason="Revision",
        )
        return context

    def test_completed_run_with_lost_finish_response_is_protected(self):
        self.assertEqual(plan(self.runner)["workspaces"], [])
        with self.assertRaises(RunConflict):
            purge_run(self.run.pk)

    def test_stale_claim_cannot_acknowledge_reporting(self):
        with self.assertRaises(RunConflict):
            acknowledge(self.runner, self.run.pk, uuid.uuid4(), self.run.finish_digest)

    def test_wrong_finish_digest_cannot_release_retention(self):
        with self.assertRaises(RunConflict):
            acknowledge(self.runner, self.run.pk, self.token, "e" * 64)

    def test_acknowledgement_is_idempotent(self):
        first = self.ack().reporting_acknowledged_at
        self.assertEqual(self.ack().reporting_acknowledged_at, first)

    def test_active_run_cannot_be_purged_even_with_old_acknowledgement(self):
        self.ack()
        self.run.state, self.run.active_slot = "active", 1
        self.run.save()
        with self.assertRaises(RunConflict):
            purge_run(self.run.pk)

    def test_pending_approval_protects_documents_and_logs(self):
        self.ack()
        self.run.approvals.create(
            operation_id=uuid.uuid4(), action={}, binding_digest="a" * 64
        )
        with self.assertRaises(RunConflict):
            purge_run(self.run.pk)

    def test_guarded_terminal_purge_removes_immutable_versions(self):
        self.documents()
        self.ack()
        result = purge_run(self.run.pk)
        self.assertEqual(result.documents_removed, 3)
        self.assertFalse(self.run.plans.exists())
        self.assertFalse(self.run.contexts.exists())
        self.assertTrue(Run.objects.filter(pk=self.run.pk).exists())

    def test_normal_document_querysets_still_forbid_deletion(self):
        self.documents()
        with self.assertRaises(ValidationError):
            self.run.plans.all().delete()

    def test_decided_approval_preserves_its_original_context(self):
        self.documents()
        self.ack()
        self.run.approvals.create(
            operation_id=uuid.uuid4(),
            action={},
            binding_digest="a" * 64,
            decision="denied",
        )
        purge_run(self.run.pk)
        self.assertEqual(self.run.contexts.count(), 1)

    def test_purge_preserves_delivery_validation_recovery_and_stage_evidence(self):
        self.ack()
        for kind in (
            "log",
            "custom-artifact",
            "verified_delivery",
            "validation",
            "progress",
            "recovery",
        ):
            RunEvidence.objects.create(
                run=self.run,
                operation_id=uuid.uuid4(),
                kind=kind,
                payload={"kind": kind},
            )
        result = purge_run(self.run.pk)
        self.assertEqual(result.artifacts_removed, 2)
        self.assertEqual(
            set(self.run.evidence.values_list("kind", flat=True)),
            {"verified_delivery", "validation", "progress", "recovery"},
        )

    def test_workspace_cleanup_rechecks_cutoff_and_ownership(self):
        self.ack()
        self.run.finished_at = timezone.now()
        self.run.save()
        with self.assertRaises(RunConflict):
            workspace_removed(self.runner, self.run.pk, "removed")

    def test_old_acknowledged_workspace_is_listed_without_server_filesystem_access(
        self,
    ):
        self.ack()
        self.assertEqual(plan(self.runner)["workspaces"], [str(self.run.pk)])
        result = workspace_removed(self.runner, self.run.pk, "missing")
        self.assertEqual(result.local_status, "missing")
        self.assertEqual(plan(self.runner)["workspaces"], [])

    def test_history_tombstone_renders_after_document_retention(self):
        self.documents()
        self.ack()
        purge_run(self.run.pk)
        response = self.client.get(reverse("runner_plan_detail", args=[self.run.pk]))
        self.assertContains(response, "history tombstones")
        self.assertContains(response, "Test task")
        self.assertIn("no-store", response.headers["Cache-Control"])

    def test_system_lists_global_blocking_reason(self):
        response = self.client.get(reverse("runner_system"))
        self.assertContains(response, "System")
        self.assertContains(response, "do not qualify native execution")

    def test_machine_retention_endpoint_requires_machine_authorization(self):
        self.assertEqual(self.client.get(reverse("runner_retention")).status_code, 401)

    def test_http_reporting_ack_requires_controller_volume_proof(self):
        self.client.defaults["HTTP_AUTHORIZATION"] = "Bearer test-token"
        self.client.defaults["HTTP_X_NQ_CONTROLLER_SECRET"] = "incorrect-proof"
        response = self.client.post(
            reverse("runner_retention_ack", args=[self.run.pk]),
            json.dumps(
                {
                    "claim_token": str(self.token),
                    "finish_digest": self.run.finish_digest,
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 409)
        self.retention.refresh_from_db()
        self.assertIsNone(self.retention.reporting_acknowledged_at)

    def test_http_reporting_ack_releases_only_its_owned_terminal_run(self):
        self.client.defaults["HTTP_AUTHORIZATION"] = "Bearer test-token"
        response = self.client.post(
            reverse("runner_retention_ack", args=[self.run.pk]),
            json.dumps(
                {
                    "claim_token": str(self.token),
                    "finish_digest": self.run.finish_digest,
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        plan_response = self.client.get(reverse("runner_retention"))
        self.assertEqual(plan_response.json()["workspaces"], [str(self.run.pk)])
        self.assertEqual(plan_response.headers["Cache-Control"], "no-store")

    def older_acknowledged_runs(self):
        runs = [
            Run(
                runner=self.runner,
                state="completed",
                finished_at=timezone.now() - timedelta(days=101),
            )
            for _ in range(100)
        ]
        Run.objects.bulk_create(runs)
        RunRetention.objects.bulk_create(
            [
                RunRetention(run=run, reporting_acknowledged_at=timezone.now())
                for run in runs
            ]
        )
        return runs

    def test_http_retention_advances_beyond_already_purged_runs(self):
        runs = self.older_acknowledged_runs()
        RunRetention.objects.filter(run__in=runs).update(
            documents_removed_at=timezone.now(), logs_removed_at=timezone.now()
        )
        self.documents()
        self.ack()
        self.client.defaults["HTTP_AUTHORIZATION"] = "Bearer test-token"
        response = self.client.post(
            reverse("runner_retention"), "{}", content_type="application/json"
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(self.run.plans.exists())
        self.retention.refresh_from_db()
        self.assertIsNotNone(self.retention.logs_removed_at)

    def test_approval_context_preservation_does_not_starve_later_document_cleanup(self):
        from .models import ActionApproval

        runs = self.older_acknowledged_runs()
        RunRetention.objects.filter(run__in=runs).update(logs_removed_at=timezone.now())
        ActionApproval.objects.bulk_create(
            [
                ActionApproval(
                    run=run,
                    operation_id=uuid.uuid4(),
                    action={},
                    binding_digest="a" * 64,
                    decision="denied",
                )
                for run in runs
            ]
        )
        self.documents()
        self.ack()
        self.client.defaults["HTTP_AUTHORIZATION"] = "Bearer test-token"
        response = self.client.post(
            reverse("runner_retention"), "{}", content_type="application/json"
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(self.run.contexts.exists())

    def test_protected_approvals_do_not_starve_workspace_and_log_plans(self):
        from .models import ActionApproval

        runs = self.older_acknowledged_runs()
        ActionApproval.objects.bulk_create(
            [
                ActionApproval(
                    run=run,
                    operation_id=uuid.uuid4(),
                    action={},
                    binding_digest="a" * 64,
                )
                for run in runs
            ]
        )
        self.ack()
        self.client.defaults["HTTP_AUTHORIZATION"] = "Bearer test-token"
        response = self.client.get(reverse("runner_retention"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["workspaces"], [str(self.run.pk)])
        self.assertEqual(response.json()["logs"], [str(self.run.pk)])

    def test_machine_token_cannot_replace_browser_csrf_for_controls(self):
        from django.test import Client

        client = Client(enforce_csrf_checks=True)
        client.defaults["HTTP_AUTHORIZATION"] = "Bearer test-token"
        response = client.post(
            reverse("runner_release_authorize"), {"authorize_live": "yes"}
        )
        self.assertEqual(response.status_code, 403)

    def test_replay_bundle_cannot_qualify(self):
        from .models import ReleaseQualification

        entry = ReleaseQualification.objects.create(
            runner=self.runner,
            repository_url=self.project.repository_url,
            scope_digest="a" * 64,
            environment_digest="b" * 64,
            code_revision="c" * 64,
            authorization_note="fixture",
            expires_at=timezone.now() + timedelta(hours=1),
        )
        with patch("runner_control.release.authorized"):
            with self.assertRaisesMessage(RunConflict, "Replay evidence"):
                complete(entry.pk, self.runner, {"mode": "replay"})
        entry.refresh_from_db()
        self.assertIsNone(entry.qualified_at)


@override_settings(NQ_CREDENTIAL_KEY=Fernet.generate_key().decode("ascii"))
class ReleaseEvidenceTests(TestCase):
    def setUp(self):
        from .configuration import configuration_document
        from .models import ReleaseQualification
        from .release import scope
        from .services import json_digest

        self.runner = RunnerIdentity.objects.create(
            name="release-test", token_digest=token_digest("fixture-token")
        )
        provision_controller(self)
        self.environment = {
            "image_digest": "sha256:" + "1" * 64,
            "operator_policy": "2" * 64,
            "kernel": "fixture-kernel",
        }
        self.entry = ReleaseQualification.objects.create(
            runner=self.runner,
            repository_url="https://github.com/example/test",
            scope_digest=scope(configuration_document()),
            environment_digest=json_digest(self.environment),
            code_revision="3" * 64,
            authorization_note="Deterministic contract fixture, not live qualification",
            expires_at=timezone.now() + timedelta(hours=1),
            evidence={
                "ui_changes": [
                    {"fields": ["paused", "schedule_mode", "reserve_enabled"]}
                ],
                "gates": [
                    {
                        "reasons": ["paused", "outside_window", "usage_unsupported"],
                        "allowed": False,
                    },
                    {"reasons": [], "allowed": True},
                ],
            },
        )
        from credentials.models import Credential

        credential = Credential(name="Fixture GitHub", provider="github", kind="token")
        credential.set_secret("fixture-test-token")
        credential.save()
        self.credential = credential
        self.workflow_check = patch(
            "runner_control.release.verify_boundary_workflow",
            return_value={"run_id": 1, "artifact_id": 1, "conclusion": "success"},
        )
        self.workflow_check.start()
        self.addCleanup(self.workflow_check.stop)
        self.success = Run.objects.create(
            runner=self.runner,
            state="completed",
            result_status="succeeded",
            finished_at=timezone.now(),
            result_url="https://github.com/example/test/pull/1",
            configuration_snapshot={
                "repository_url": self.entry.repository_url,
                "effective": {"repository_credential_id": self.credential.pk},
            },
        )
        self.failure = Run.objects.create(
            runner=self.runner,
            state="completed",
            result_status="failed",
            finished_at=timezone.now(),
            error_summary="Validation failed with exit code 2; protected log run/execution",
            configuration_snapshot={
                "repository_url": self.entry.repository_url,
                "effective": {"repository_credential_id": self.credential.pk},
            },
        )
        context = self.success.contexts.create(
            operation_id=uuid.uuid4(),
            document={},
            digest="a" * 64,
            reason="Fixture",
            sequence=1,
            inspected_base_commit="b" * 40,
            resolved_base_branch="main",
            discussion_digest="c" * 64,
        )
        self.success.plans.create(
            context=context,
            operation_id=uuid.uuid4(),
            document={},
            digest="d" * 64,
            reason="Fixture",
            version=1,
        )
        self.success.evidence.create(
            operation_id=uuid.uuid4(),
            kind="verified_delivery",
            payload={
                "commit_sha": "4" * 40,
                "implementation": {"validation": [{"exit_code": 0}]},
            },
        )
        self.failure.evidence.create(
            operation_id=uuid.uuid4(), kind="execution_log", payload={"exit_code": 2}
        )
        observation = {
            "url": self.success.result_url,
            "commit_sha": "4" * 40,
            "repository_url": self.entry.repository_url,
            "head_branch": "nq/test",
            "matching_pr_count": 1,
        }
        self.report = {
            "mode": "live",
            "code_revision": self.entry.code_revision,
            "environment_digest": self.entry.environment_digest,
            "boundary": {
                "environment": self.environment,
                "command": ["python", "-m", "coding_runner.verify", "--boundary"],
                "exit_code": 0,
                "output_digest": "5" * 64,
                "workflow_run_url": "https://github.com/example/test/actions/runs/1",
            },
            "success_run_id": str(self.success.pk),
            "failure_run_id": str(self.failure.pk),
            "pr_observations": [
                {**observation, "controller_boot_id": "before"},
                {**observation, "controller_boot_id": "after"},
            ],
            "model_responses": [
                {
                    "backend": "codex",
                    "credential_id": 1,
                    "model": "fixture",
                    "secret_version": 1,
                    "response_id": "fixture-provider-response",
                    "transport": "codex-responses-structured-proposals-v1",
                }
            ],
        }

    def test_complete_contract_records_scoped_qualification(self):
        result = complete(self.entry.pk, self.runner, self.report)
        self.assertIsNotNone(result.qualified_at)
        self.assertEqual(result.evidence["live_report"], self.report)

    def test_missing_boundary_test_execution_blocks_release(self):
        self.report["boundary"]["exit_code"] = 1
        with self.assertRaisesMessage(RunConflict, "actually pass"):
            complete(self.entry.pk, self.runner, self.report)

    def test_unbound_image_blocks_release(self):
        self.report["boundary"]["environment"]["image_digest"] = "unbound"
        with self.assertRaises(RunConflict):
            complete(self.entry.pk, self.runner, self.report)

    def test_same_controller_boot_cannot_prove_restart(self):
        self.report["pr_observations"][1]["controller_boot_id"] = "before"
        with self.assertRaisesMessage(RunConflict, "restart"):
            complete(self.entry.pk, self.runner, self.report)

    def test_multiple_pull_requests_block_qualification(self):
        self.report["pr_observations"][1]["matching_pr_count"] = 2
        with self.assertRaisesMessage(RunConflict, "exactly one"):
            complete(self.entry.pk, self.runner, self.report)

    def test_undocumented_failure_blocks_qualification(self):
        self.failure.error_summary = ""
        self.failure.save()
        with self.assertRaisesMessage(RunConflict, "useful diagnostics"):
            complete(self.entry.pk, self.runner, self.report)

    def test_scope_change_invalidates_previously_qualified_release(self):
        from .configuration import configuration_document
        from .settings_models import RunnerSettings

        complete(self.entry.pk, self.runner, self.report)
        self.assertFalse(configuration_document()["execution_ready"])
        settings = RunnerSettings.objects.get(pk=1)
        settings.research_model = "changed-model"
        settings.save()
        self.assertFalse(configuration_document()["execution_ready"])

    def test_pause_control_does_not_invalidate_qualification(self):
        from .configuration import configuration_document
        from .settings_models import RunnerSettings

        complete(self.entry.pk, self.runner, self.report)
        settings = RunnerSettings.objects.get(pk=1)
        settings.paused = True
        settings.save()
        self.assertFalse(configuration_document()["execution_ready"])
        self.assertFalse(configuration_document()["global"]["enabled"])

    def test_qualified_evidence_cannot_be_rewritten(self):
        complete(self.entry.pk, self.runner, self.report)
        self.report["model_responses"] = []
        with self.assertRaisesMessage(RunConflict, "immutable"):
            complete(self.entry.pk, self.runner, self.report)

    def test_expired_authorization_fails_closed(self):
        self.entry.expires_at = timezone.now() - timedelta(seconds=1)
        self.entry.save()
        with self.assertRaisesMessage(RunConflict, "expired"):
            complete(self.entry.pk, self.runner, self.report)

    def post_release_report(self):
        self.client.defaults["HTTP_AUTHORIZATION"] = "Bearer fixture-token"
        return self.client.post(
            reverse("runner_release_suite", args=[self.entry.pk]),
            json.dumps({"report": self.report}),
            content_type="application/json",
        )

    def test_http_secret_response_identity_cannot_qualify_or_persist(self):
        self.report["model_responses"][0]["response_id"] = "fixture-test-token"
        response = self.post_release_report()
        self.assertEqual(response.status_code, 409)
        self.assertNotContains(response, "fixture-test-token", status_code=409)
        self.entry.refresh_from_db()
        self.assertIsNone(self.entry.qualified_at)
        self.assertNotIn("live_report", self.entry.evidence)

    def test_http_null_response_identity_cannot_poison_qualification(self):
        self.report["model_responses"][0]["response_id"] = "response\x00identity"
        self.assertEqual(self.post_release_report().status_code, 400)
        self.entry.refresh_from_db()
        self.assertIsNone(self.entry.qualified_at)

    def test_http_surrogate_response_identity_cannot_poison_qualification(self):
        self.report["model_responses"][0]["response_id"] = "response\ud800identity"
        self.assertEqual(self.post_release_report().status_code, 400)
        self.entry.refresh_from_db()
        self.assertIsNone(self.entry.qualified_at)

    def test_http_oversized_response_identity_cannot_qualify(self):
        self.report["model_responses"][0]["response_id"] = "r" * 201
        self.assertEqual(self.post_release_report().status_code, 409)
        self.entry.refresh_from_db()
        self.assertIsNone(self.entry.qualified_at)

    def test_http_unhashable_response_account_is_rejected(self):
        self.report["model_responses"][0]["model"] = ["invalid"]
        self.assertEqual(self.post_release_report().status_code, 409)
        self.entry.refresh_from_db()
        self.assertIsNone(self.entry.qualified_at)


@override_settings(NQ_CREDENTIAL_KEY=Fernet.generate_key().decode("ascii"))
class OperatorPresentationTests(TestCase):
    def setUp(self):
        from credentials.models import Credential
        from .configuration import configuration_document
        from .models import ActionApproval, RunnerHealth
        from .settings_models import RunnerSettings

        self.runner = RunnerIdentity.objects.create(
            name="Operator test", token_digest=token_digest("operator-test-token")
        )
        provision_controller(self)
        self.client.defaults["HTTP_AUTHORIZATION"] = "Bearer operator-test-token"
        self.current = Credential(
            name="Current selection", provider="codex", kind="token"
        )
        self.current.set_secret("previous-global-selection")
        self.current.save()
        self.current.set_secret("current-global-selection")
        self.current.save()
        self.historical = Credential(
            name="Run selection", provider="codex", kind="token"
        )
        self.historical.set_secret("previous-run-selection")
        self.historical.save()
        self.historical.set_secret("current-run-selection")
        self.historical.save()
        self.settings = RunnerSettings.objects.get(pk=1)
        self.settings.research_backend = self.settings.implementation_backend = "codex"
        self.settings.research_model = self.settings.implementation_model = (
            "fixture-model"
        )
        self.settings.research_credential = self.settings.implementation_credential = (
            self.current
        )
        self.settings.save()
        self.project = Project.objects.create(
            name="Fixture project", repository_url="https://github.com/example/test"
        )
        self.item = enqueue_item(project=self.project, title="previous-run-selection")
        self.run = Run.objects.create(
            runner=self.runner,
            item=self.item,
            state="completed",
            stage="finished",
            finished_at=timezone.now(),
            result_status="failed",
            error_summary="current-run-selection",
            wait_reason="previous-run-selection",
            result_url="https://github.com/example/test/pull/1?detail=current-run-selection",
            configuration_snapshot={
                "effective": {"research_credential_id": self.historical.pk},
                "instructions": "previous-run-selection and current-run-selection",
            },
        )
        self.delivery = self.run.evidence.create(
            operation_id=uuid.uuid4(),
            kind="delivery",
            payload={"diagnostic": "previous-run-selection"},
        )
        ActionApproval.objects.create(
            run=self.run,
            operation_id=uuid.uuid4(),
            action={"argv": ["current-run-selection"]},
            decision="denied",
            decision_note="previous-run-selection",
            binding_digest="a" * 64,
        )
        self.health = RunnerHealth.objects.create(
            runner=self.runner,
            report={
                "free_bytes": 2000000000,
                "removed": 0,
                "missing": 0,
                "busy": 0,
                "blocking_reason": "previous-global-selection",
            },
        )
        now = timezone.now()
        from .gate_models import RunnerGateEvidence

        self.gate = RunnerGateEvidence.objects.create(
            runner=self.runner,
            checked_at=now,
            report={
                "schema_version": 1,
                "reserve_enabled": True,
                "minimum_remaining_usage_percent": 40,
                "interval": None,
                "next_boundary": None,
                "tasks_started": 0,
                "run_id": str(self.run.pk),
                "configuration_revision": configuration_document()["revision"],
                "next_check_at": (now + timedelta(seconds=60)).isoformat(),
                "checked_at": now.isoformat(),
                "allowed": False,
                "checkpoint": "periodic",
                "reasons": ["usage_reserve"],
                "readings": [
                    {
                        "backend": "codex",
                        "credential_id": self.current.pk,
                        "status": "reserve",
                        "windows": [
                            {
                                "window_id": "previous-run-selection current-global-selection",
                                "remaining_percent": 20,
                                "status": "reserve",
                                "observed_at": now.isoformat(),
                                "reset_at": None,
                            }
                        ],
                    }
                ],
            },
        )

    def assert_safe(self, response, *, global_values=False):
        self.assertEqual(response.status_code, 200, response.content[:1000])
        self.assertNotContains(response, "previous-run-selection")
        self.assertNotContains(response, "current-run-selection")
        if global_values:
            self.assertNotContains(response, "previous-global-selection")
            self.assertNotContains(response, "current-global-selection")
        self.assertIn("no-store", response["Cache-Control"])

    def test_detail_redacts_expanded_fields_without_rewriting_evidence(self):
        response = self.client.get(reverse("runner_plan_detail", args=[self.run.pk]))
        self.assert_safe(response)
        self.assertContains(response, "[redacted]")
        self.run.refresh_from_db()
        self.delivery.refresh_from_db()
        self.assertEqual(self.run.error_summary, "current-run-selection")
        self.assertEqual(self.delivery.payload["diagnostic"], "previous-run-selection")
        self.assertEqual(
            self.run.approvals.get().decision_note, "previous-run-selection"
        )

    def test_system_scrubs_current_global_and_owned_historical_selections(self):
        response = self.client.get(reverse("runner_system"))
        self.assert_safe(response, global_values=True)
        self.assertContains(response, "[redacted]")
        self.health.refresh_from_db()
        self.assertEqual(
            self.health.report["blocking_reason"], "previous-global-selection"
        )

    def test_gate_api_scrubs_historical_run_and_current_global_values(self):
        response = self.client.get(reverse("runner_gates"))
        self.assert_safe(response, global_values=True)
        window = response.json()["evidence"]["report"]["readings"][0]["windows"][0]
        self.assertEqual(window["window_id"], "[redacted] [redacted]")

    def test_unavailable_selected_key_hides_expanded_content(self):
        with override_settings(NQ_CREDENTIAL_KEY=""):
            detail = self.client.get(reverse("runner_plan_detail", args=[self.run.pk]))
            history = self.client.get(reverse("runner_system"))
            gate = self.client.get(reverse("runner_gates"))
        self.assert_safe(detail)
        self.assert_safe(history, global_values=True)
        self.assertContains(detail, "unavailable")
        self.assertContains(history, "unavailable")
        self.assertEqual(gate.status_code, 400)
        self.assertNotContains(gate, "previous-run-selection", status_code=400)

    def test_unselected_broken_credential_is_not_decrypted(self):
        from credentials.models import Credential

        Credential.objects.create(
            name="Unselected corrupt",
            provider="codex",
            kind="token",
            encrypted_payload="invalid",
        )
        self.assert_safe(self.client.get(reverse("runner_system")), global_values=True)
        self.assert_safe(
            self.client.get(reverse("runner_plan_detail", args=[self.run.pk]))
        )

    def test_health_api_redacts_selected_and_machine_secrets_before_storage(self):
        from .test_support import CONTROLLER_SECRET

        report = {
            "free_bytes": 2000000000,
            "minimum_free_bytes": 1000000000,
            "removed": 0,
            "missing": 0,
            "busy": 0,
            "blocking_reason": "previous-global-selection operator-test-token "
            + CONTROLLER_SECRET,
        }
        response = self.client.post(
            reverse("runner_health"),
            json.dumps(report),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.health.refresh_from_db()
        self.assertEqual(
            self.health.report["blocking_reason"], "[redacted] [redacted] [redacted]"
        )

    def qualification(self):
        from .configuration import configuration_document
        from .models import ReleaseQualification
        from .release import scope

        return ReleaseQualification.objects.create(
            runner=self.runner,
            repository_url=self.project.repository_url,
            scope_digest=scope(configuration_document()),
            environment_digest="a" * 64,
            code_revision="b" * 64,
            authorization_note="Authorized test; previous-global-selection",
            blocker="previous-run-selection",
            expires_at=timezone.now() + timedelta(hours=1),
            evidence={
                "live_report": {
                    "success_run_id": str(self.run.pk),
                    "model_responses": [
                        {"response_id": "previous-run-selection", "secret_version": 2}
                    ],
                }
            },
        )

    def test_release_api_scrubs_selected_values_and_preserves_provenance(self):
        entry = self.qualification()
        response = self.client.get(reverse("runner_release_suite", args=[entry.pk]))
        self.assert_safe(response, global_values=True)
        result = response.json()["qualification"]
        trace = result["evidence"]["live_report"]["model_responses"][0]
        self.assertEqual(trace["secret_version"], 2)
        self.assertEqual(trace["response_id"], "[redacted]")
        self.assertTrue(result["evidence_presentation_only"])
        self.assertEqual(result["code_revision"], entry.code_revision)
        entry.refresh_from_db()
        self.assertEqual(
            entry.evidence["live_report"]["model_responses"][0]["response_id"],
            "previous-run-selection",
        )

    def test_settings_release_and_gate_sections_use_safe_presentation(self):
        self.qualification()
        response = self.client.get(reverse("api_settings"))
        self.assert_safe(response, global_values=True)
        self.assertContains(response, "Controlled release verification")
        self.assertContains(response, "[redacted]")

    def test_project_selected_credentials_are_included_without_report_run(self):
        from .settings_models import ProjectRunnerSettings

        ProjectRunnerSettings.objects.create(
            project=self.project,
            overrides={
                "research_credential_id": {
                    "mode": "replace",
                    "value": self.historical.pk,
                }
            },
        )
        self.gate.report["run_id"] = None
        self.gate.save(update_fields=["report"])
        response = self.client.get(reverse("runner_gates"))
        self.assert_safe(response, global_values=True)

    def test_missing_project_selected_credential_hides_global_reports(self):
        from .settings_models import ProjectRunnerSettings

        ProjectRunnerSettings.objects.create(
            project=self.project,
            overrides={"research_credential_id": {"mode": "replace", "value": 999999}},
        )
        response = self.client.get(reverse("runner_system"))
        self.assert_safe(response, global_values=True)
        self.assertContains(response, "unavailable")

    def test_machine_configuration_keeps_executable_contract(self):
        from .configuration import configuration_document

        expected = configuration_document()
        response = self.client.get(reverse("runner_configuration"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), expected)
        reference = next(
            row
            for row in response.json()["credentials"]
            if row["id"] == self.current.pk
        )
        self.assertEqual(reference["secret_version"], 2)

    def test_settings_form_withholds_secret_values_without_changing_configuration(self):
        self.settings.research_instructions = "previous-global-selection"
        self.settings.save()
        response = self.client.get(reverse("api_settings"))
        self.assert_safe(response, global_values=True)
        self.assertContains(response, "Selected credential content is withheld")
        self.assertNotContains(response, "Save coding runner settings")
        machine = self.client.get(reverse("runner_configuration"))
        self.assertEqual(
            machine.json()["global"]["research_instructions"],
            "previous-global-selection",
        )
        self.settings.refresh_from_db()
        self.assertEqual(
            self.settings.research_instructions, "previous-global-selection"
        )

    def test_health_report_keeps_its_owned_run_redaction_after_completion(self):
        self.run.state, self.run.active_slot = "active", 1
        self.run.save(update_fields=["state", "active_slot"])
        report = {
            "free_bytes": 2000000000,
            "minimum_free_bytes": 1000000000,
            "removed": 0,
            "missing": 0,
            "busy": 0,
            "blocking_reason": "previous-run-selection current-global-selection",
        }
        response = self.client.post(
            reverse("runner_health"),
            json.dumps(report),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.health.refresh_from_db()
        self.assertEqual(self.health.report["run_id"], str(self.run.pk))
        self.assertEqual(self.health.report["blocking_reason"], "[redacted] [redacted]")
        self.run.state, self.run.active_slot = "completed", None
        self.run.save(update_fields=["state", "active_slot"])
        self.health.report["blocking_reason"] = "previous-run-selection"
        self.health.save(update_fields=["report"])
        self.assert_safe(self.client.get(reverse("runner_system")), global_values=True)

    def test_release_metadata_exception_does_not_unmask_other_secret_fields(self):
        entry = self.qualification()
        entry.evidence["legacy"] = {
            "password": "withhold-me",
            "secret_version": "withhold-string-version",
        }
        entry.save(update_fields=["evidence"])
        response = self.client.get(reverse("runner_release_suite", args=[entry.pk]))
        self.assertEqual(response.status_code, 200)
        legacy = response.json()["qualification"]["evidence"]["legacy"]
        self.assertEqual(
            legacy, {"password": "[redacted]", "secret_version": "[redacted]"}
        )

    def test_selected_current_credential_name_cannot_leak_through_runner_choices(self):
        self.current.name = "Current label: current-global-selection"
        self.current.save(update_fields=["name"])
        response = self.client.get(reverse("api_settings"))
        self.assert_safe(response, global_values=True)
        self.assertContains(response, "Selected credential content is withheld")
        self.assertNotContains(response, "Save coding runner settings")

    def test_selected_historical_credential_name_cannot_leak_through_api_choices(self):
        from credentials.models import Credential

        Credential.objects.create(
            name="Import label: previous-global-selection",
            provider="github",
            kind="token",
            encrypted_payload="unselected-invalid",
        )
        response = self.client.get(reverse("api_settings"))
        self.assert_safe(response, global_values=True)
        self.assertContains(response, "Selected credential content is withheld")
        self.assertNotContains(response, ">Save settings</button>", html=False)

    def test_missing_key_hides_labels_in_both_settings_forms(self):
        from credentials.models import Credential

        self.current.name = "Current label: current-global-selection"
        self.current.save(update_fields=["name"])
        Credential.objects.create(
            name="Import label: previous-global-selection",
            provider="github",
            kind="token",
            encrypted_payload="unselected-invalid",
        )
        with override_settings(NQ_CREDENTIAL_KEY=""):
            response = self.client.get(reverse("api_settings"))
        self.assert_safe(response, global_values=True)
        self.assertContains(response, "unavailable")
        self.assertNotContains(response, "Save coding runner settings")
        self.assertNotContains(response, ">Save settings</button>", html=False)

    def test_unselected_invalid_credential_does_not_hide_safe_choice_labels(self):
        from credentials.models import Credential

        Credential.objects.create(
            name="Unselected safe label",
            provider="github",
            kind="token",
            encrypted_payload="invalid",
        )
        response = self.client.get(reverse("api_settings"))
        self.assert_safe(response, global_values=True)
        self.assertContains(response, "Unselected safe label")
        self.assertContains(response, "Save coding runner settings")
        self.assertContains(response, ">Save settings</button>", html=False)

    def escaped_selected_token(self):
        token = 'synthetic-quote-"-slash-\\-unicode-☃'
        self.current.set_secret(token)
        self.current.save()
        return token

    def assert_escaped_form_withheld(self, response, token):
        from html import unescape

        self.assertEqual(response.status_code, 200)
        decoded = unescape(response.content.decode())
        self.assertNotIn(token, decoded)
        self.assertNotIn(json.dumps(token)[1:-1], decoded)
        self.assertNotContains(response, "Save coding runner settings")
        self.assertIn("no-store", response["Cache-Control"])

    def test_json_settings_withhold_escaped_current_selected_material(self):
        token = self.escaped_selected_token()
        commands = ["/usr/bin/echo " + token]
        self.settings.setup_commands = commands
        self.settings.save()
        response = self.client.get(reverse("api_settings"))
        self.assert_escaped_form_withheld(response, token)
        self.assertContains(response, "Selected credential content is withheld")
        self.settings.refresh_from_db()
        self.assertEqual(self.settings.setup_commands, commands)
        machine = self.client.get(reverse("runner_configuration"))
        self.assertEqual(machine.json()["global"]["setup_commands"], commands)

    def test_json_settings_withhold_escaped_historical_selected_material(self):
        token = self.escaped_selected_token()
        self.current.set_secret("new-current-value")
        self.current.save()
        self.settings.setup_commands = ["/usr/bin/echo " + token]
        self.settings.save()
        response = self.client.get(reverse("api_settings"))
        self.assert_escaped_form_withheld(response, token)
        self.assertContains(response, "Selected credential content is withheld")
        self.settings.refresh_from_db()
        self.assertEqual(self.settings.setup_commands, ["/usr/bin/echo " + token])

    def test_bound_json_settings_inspect_decoded_selected_material(self):
        token = self.escaped_selected_token()
        response = self.client.post(
            reverse("api_settings"),
            {
                "section": "runner",
                "runner-setup_commands": json.dumps(["/usr/bin/echo " + token]),
            },
        )
        self.assert_escaped_form_withheld(response, token)
        self.assertContains(response, "Selected credential content is withheld")
        self.settings.refresh_from_db()
        self.assertEqual(self.settings.setup_commands, [])

    def test_invalid_bound_json_withholds_partially_escaped_selected_material(self):
        token = self.escaped_selected_token()
        invalid = json.dumps(["/usr/bin/echo " + token])[:-1]
        response = self.client.post(
            reverse("api_settings"),
            {"section": "runner", "runner-setup_commands": invalid},
        )
        self.assert_escaped_form_withheld(response, token)
        self.assertContains(response, "Invalid JSON content is withheld")
        self.settings.refresh_from_db()
        self.assertEqual(self.settings.setup_commands, [])

    def test_unselected_corrupt_credential_does_not_block_safe_json_form(self):
        from credentials.models import Credential

        Credential.objects.create(
            name="Unselected corrupt JSON fixture",
            provider="codex",
            kind="token",
            encrypted_payload="invalid",
        )
        self.settings.setup_commands = ['echo safe-quote-"-slash-\\-unicode-☃']
        self.settings.save()
        response = self.client.get(reverse("api_settings"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Save coding runner settings")

    def test_project_editor_redacts_inherited_effective_values_without_rewriting(self):
        self.settings.research_instructions = "current-global-selection"
        self.settings.save()
        response = self.client.get(reverse("edit_project", args=[self.project.pk]))
        self.assert_safe(response, global_values=True)
        self.assertContains(response, "[redacted]")
        self.assertContains(response, "Save changes")
        self.settings.refresh_from_db()
        self.assertEqual(
            self.settings.research_instructions, "current-global-selection"
        )

    def test_project_create_hides_selected_material_in_runner_choice_labels(self):
        self.current.name = "Runner label: current-global-selection"
        self.current.save(update_fields=["name"])
        response = self.client.get(reverse("project_list"))
        self.assert_safe(response, global_values=True)
        self.assertContains(response, "Selected credential content is withheld")
        self.assertNotContains(response, ">Create project</button>", html=False)

    def test_project_create_hides_selected_material_in_import_choice_labels(self):
        from credentials.models import Credential

        Credential.objects.create(
            name="Import label: previous-global-selection",
            provider="github",
            kind="token",
            encrypted_payload="unselected-invalid",
        )
        response = self.client.get(reverse("project_list"))
        self.assert_safe(response, global_values=True)
        self.assertNotContains(response, ">Create project</button>", html=False)

    def test_project_editor_withholds_escaped_historical_json_overrides(self):
        from .settings_models import ProjectRunnerSettings

        token = self.escaped_selected_token()
        self.current.set_secret("next-current-project-value")
        self.current.save()
        commands = ["echo " + token]
        overrides = {"setup_commands": {"mode": "replace", "value": commands}}
        saved = ProjectRunnerSettings.objects.create(
            project=self.project, overrides=overrides
        )
        response = self.client.get(reverse("edit_project", args=[self.project.pk]))
        self.assert_escaped_form_withheld(response, token)
        self.assertNotContains(response, ">Save changes</button>", html=False)
        self.assertContains(response, "[redacted]")
        saved.refresh_from_db()
        self.assertEqual(saved.overrides, overrides)
        machine = self.client.get(reverse("runner_configuration"))
        project = next(
            row
            for row in machine.json()["projects"]
            if row["project_id"] == self.project.pk
        )
        self.assertEqual(project["effective"]["setup_commands"], commands)

    def test_project_create_withholds_invalid_json_with_escaped_selected_material(self):
        token = self.escaped_selected_token()
        invalid = json.dumps(["echo " + token])[:-1]
        response = self.client.post(
            reverse("project_list"),
            {
                "name": "",
                "runner-setup_commands_mode": "replace",
                "runner-setup_commands": invalid,
            },
        )
        self.assert_escaped_form_withheld(response, token)
        self.assertNotContains(response, ">Create project</button>", html=False)
        self.assertContains(response, "Invalid JSON content is withheld")
        self.assertEqual(Project.objects.count(), 1)

    def test_project_routes_hide_selected_labels_and_values_when_key_is_missing(self):
        self.current.name = "Runner label: current-global-selection"
        self.current.save(update_fields=["name"])
        self.project.name = "Project: previous-global-selection"
        self.project.save()
        with override_settings(NQ_CREDENTIAL_KEY=""):
            created = self.client.get(reverse("project_list"))
            edited = self.client.get(reverse("edit_project", args=[self.project.pk]))
        self.assert_safe(created, global_values=True)
        self.assert_safe(edited, global_values=True)
        self.assertContains(created, "unavailable")
        self.assertContains(edited, "unavailable")
        self.assertNotContains(created, ">Create project</button>", html=False)
        self.assertNotContains(edited, ">Save changes</button>", html=False)

    def test_safe_project_forms_remain_editable_without_decrypting_unselected_credentials(
        self,
    ):
        from credentials.models import Credential

        Credential.objects.create(
            name="Unselected project label",
            provider="github",
            kind="token",
            encrypted_payload="invalid",
        )
        created = self.client.get(reverse("project_list"))
        edited = self.client.get(reverse("edit_project", args=[self.project.pk]))
        self.assert_safe(created, global_values=True)
        self.assert_safe(edited, global_values=True)
        self.assertContains(created, "Unselected project label")
        self.assertContains(created, ">Create project</button>", html=False)
        self.assertContains(edited, ">Save changes</button>", html=False)

    def test_project_list_redacts_metadata_copies_without_rewriting_project(self):
        self.project.name = "Project: previous-global-selection"
        self.project.save()
        response = self.client.get(reverse("project_list"))
        self.assert_safe(response, global_values=True)
        self.assertContains(response, "Project: [redacted]")
        self.assertContains(response, ">Create project</button>", html=False)
        self.project.refresh_from_db()
        self.assertEqual(self.project.name, "Project: previous-global-selection")
