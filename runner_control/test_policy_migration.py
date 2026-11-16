from uuid import uuid4

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from django.utils import timezone

from .models import ActionApproval, Run, RunnerSettings


class ExecutionPolicyMigrationTests(TransactionTestCase):
    def restore_schema(self):
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())

    def migrate_legacy_run(self, *, consumed=False, enabled=True):
        self.addCleanup(self.restore_schema)
        executor = MigrationExecutor(connection)
        target = [("runner_control", "0008_runner_health")]
        executor.migrate(target)
        apps = executor.loader.project_state(target).apps
        settings, _ = apps.get_model(
            "runner_control", "RunnerSettings"
        ).objects.get_or_create(pk=1)
        settings.enabled = enabled
        settings.save()
        project = apps.get_model("projects", "Project").objects.create(
            name="Legacy", repository_url="https://github.com/example/legacy.git"
        )
        item = apps.get_model("work_queue", "QueueItem").objects.create(
            project=project,
            title="Interrupted work",
            status="running",
            claim_token=uuid4(),
        )
        runner = apps.get_model("runner_control", "RunnerIdentity").objects.create(
            name="legacy", token_digest="a" * 64
        )
        run = apps.get_model("runner_control", "Run").objects.create(
            runner=runner,
            item=item,
            state="active",
            active_slot=1,
            stage="waiting",
            wait_reason="Human approval required",
            claim_digest="b" * 64,
        )
        action = apps.get_model("runner_control", "ActionApproval").objects.create(
            run=run,
            operation_id=uuid4(),
            action={"argv": ["legacy-command"]},
            binding_digest="c" * 64,
            decision="approved" if consumed else "pending",
            consumed_at=timezone.now() if consumed else None,
            execution_id=uuid4() if consumed else None,
        )
        self.restore_schema()
        return Run.objects.get(pk=run.pk), ActionApproval.objects.get(pk=action.pk)

    def test_pending_wait_preserves_fence_and_original_approval(self):
        run, approval = self.migrate_legacy_run()
        self.assertEqual(run.state, "active")
        self.assertEqual(run.active_slot, 1)
        self.assertIsNotNone(run.cancellation_requested_at)
        self.assertIsNone(run.cancellation_confirmed_at)
        self.assertIn("Confirm termination", run.wait_reason)
        self.assertEqual(approval.decision, "pending")
        self.assertEqual(run.item.status, "running")
        self.assertIsNotNone(run.item.claim_token)
        self.assertFalse(RunnerSettings.objects.get(pk=1).enabled)
        self.assertEqual(
            run.evidence.get(kind="legacy_execution_stopped").payload,
            {"needs_retry": True, "termination_confirmed": False},
        )

    def test_consumed_unknown_command_remains_owned_until_termination(self):
        run, approval = self.migrate_legacy_run(consumed=True)
        self.assertEqual(run.state, "active")
        self.assertEqual(run.active_slot, 1)
        self.assertIsNone(run.cancellation_confirmed_at)
        self.assertEqual(approval.decision, "approved")
        self.assertIsNotNone(approval.execution_id)
        self.assertIsNotNone(approval.consumed_at)

    def test_disabled_installation_remains_disabled(self):
        self.migrate_legacy_run(enabled=False)
        self.assertFalse(RunnerSettings.objects.get(pk=1).enabled)
