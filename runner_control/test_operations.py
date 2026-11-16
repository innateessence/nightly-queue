import uuid
from datetime import timedelta
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import TestCase
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


class RetentionTests(TestCase):
    def setUp(self):
        self.runner = RunnerIdentity.objects.create(
            name="operations", token_digest=token_digest("test-token")
        )
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
        self.assertEqual(response.headers["Cache-Control"].split(",")[0], "max-age=0")

    def test_history_lists_runs_and_global_blocking_reason(self):
        response = self.client.get(reverse("runner_plan_history"))
        self.assertContains(response, "Run history")
        self.assertContains(response, "Release verification blocks normal execution")

    def test_machine_retention_endpoint_requires_machine_authorization(self):
        self.assertEqual(self.client.get(reverse("runner_retention")).status_code, 401)

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


class ReleaseEvidenceTests(TestCase):
    def setUp(self):
        from .configuration import configuration_document
        from .models import ReleaseQualification
        from .release import scope
        from .services import json_digest

        self.runner = RunnerIdentity.objects.create(
            name="release-test", token_digest=token_digest("fixture-token")
        )
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
        self.success = Run.objects.create(
            runner=self.runner,
            state="completed",
            result_status="succeeded",
            finished_at=timezone.now(),
            result_url="https://github.com/example/test/pull/1",
            configuration_snapshot={"repository_url": self.entry.repository_url},
        )
        self.failure = Run.objects.create(
            runner=self.runner,
            state="completed",
            result_status="failed",
            finished_at=timezone.now(),
            error_summary="Validation failed with exit code 2; protected log run/execution",
            configuration_snapshot={"repository_url": self.entry.repository_url},
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
        self.assertTrue(configuration_document()["execution_ready"])
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
        self.assertTrue(configuration_document()["execution_ready"])
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
