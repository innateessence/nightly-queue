import copy
from datetime import timedelta
from unittest.mock import Mock, patch
from uuid import uuid4

from django.test import TestCase, override_settings

from credentials.test_models import TEST_KEY
from .delivery_recovery import recover_delivery
from .models import NativeDeliveryRecovery, RunRetention
from .native_services import update_status
from .retention import acknowledge
from .services import RunConflict, finish_run
from .test_automatic_merge import MergeProtocolMixin


@override_settings(NQ_CREDENTIAL_KEY=TEST_KEY)
class DeliveryRecoveryTests(MergeProtocolMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.github_source()
        self.complete_delivery()
        self.terminate()
        self.run = finish_run(
            runner=self.runner,
            run_id=self.run.pk,
            claim_token=self.claim,
            status="failed",
            outcome="failed",
            error_summary="Remote read failed.",
        )
        acknowledge(self.runner, self.run.pk, self.claim, self.run.finish_digest)
        publication = self.run.evidence.filter(kind="native_delivery").last()
        self.expected = {
            "expected_finish_digest": self.run.finish_digest,
            "expected_publication_id": publication.payload["publication_id"],
            "expected_head_sha": self.workspace["commit_sha"],
        }
        self.operation_id = uuid4()
        workspace = self.workspace
        raw = {
            "number": self.pull["number"],
            "html_url": self.pull["url"],
            "state": "open",
            "merged": False,
            "draft": False,
            "head": {
                "repo": {"id": 123, "full_name": "example/configured"},
                "ref": workspace["branch"],
                "sha": workspace["commit_sha"],
            },
            "base": {
                "repo": {"id": 123, "full_name": "example/configured"},
                "ref": "main",
                "sha": workspace["base_revision"],
            },
        }
        self.provider = Mock()
        self.provider.pull.return_value = raw
        self.provider_patch = patch(
            "runner_control.merge_services.provider", return_value=self.provider
        )
        self.provider_patch.start()
        self.addCleanup(self.provider_patch.stop)

    def recover(self, **changes):
        return recover_delivery(
            runner=self.runner,
            run_id=self.run.pk,
            operation_id=changes.pop("operation_id", self.operation_id),
            expected={**self.expected, **changes},
        )

    def test_recovery_archives_failed_result_and_preserves_original_session(self):
        before = self.run.evidence.count()
        self.assertEqual(self.run.item.result_url, self.pull["url"])
        run, recovery = self.recover()
        self.assertEqual(recovery.archive["item_result_url"], self.pull["url"])
        self.assertEqual(
            recovery.archive["finish_digest"], self.expected["expected_finish_digest"]
        )
        self.assertEqual(recovery.archive["native_outcome"], "failed")
        self.assertTrue(recovery.archive["reporting_acknowledged_at"])
        self.assertEqual(run.state, "active")
        self.assertNotEqual(run.item.claim_token, self.claim)
        self.assertIsNone(RunRetention.objects.get(run=run).reporting_acknowledged_at)
        self.assertEqual(run.evidence.count(), before)
        self.assertEqual(
            recovery.resume["agent"]["session_id"],
            self.evidence["runtime"]["session_id"],
        )
        self.assertEqual(
            recovery.resume["deadline"],
            (
                run.created_at
                + timedelta(
                    seconds=run.configuration_snapshot["global"]["task_timeout_seconds"]
                )
            ).isoformat(),
        )

    def test_lost_recovery_response_reuses_one_generation_and_claim(self):
        _, first = self.recover()
        _, second = self.recover()
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(first.claim_token, second.claim_token)
        self.assertEqual(NativeDeliveryRecovery.objects.count(), 1)
        self.provider.pull.assert_called_once()

    def test_different_recovery_operation_cannot_repeat_generation(self):
        self.recover()
        with self.assertRaises(RunConflict):
            self.recover(operation_id=uuid4())

    def test_stale_expected_result_cannot_activate_recovery(self):
        with self.assertRaises(RunConflict):
            self.recover(expected_finish_digest="a" * 64)
        self.run.refresh_from_db()
        self.assertEqual(self.run.state, "completed")
        self.assertFalse(NativeDeliveryRecovery.objects.exists())

    def test_original_deadline_cannot_be_extended(self):
        future = self.run.created_at + timedelta(
            seconds=self.run.configuration_snapshot["global"]["task_timeout_seconds"]
            + 1
        )
        with patch(
            "runner_control.delivery_recovery.timezone.now", return_value=future
        ):
            with self.assertRaises(RunConflict):
                self.recover()

    def test_missing_positive_ack_blocks_recovery(self):
        RunRetention.objects.filter(run=self.run).update(reporting_acknowledged_at=None)
        with self.assertRaises(RunConflict):
            self.recover()

    def test_changed_provider_head_rolls_back_recovery(self):
        self.provider.pull.return_value["head"]["sha"] = "a" * 40
        with self.assertRaises(RunConflict):
            self.recover()
        self.run.refresh_from_db()
        self.assertEqual(self.run.state, "completed")
        self.run.item.refresh_from_db()
        self.assertEqual(self.run.item.result_url, self.pull["url"])
        self.assertTrue(
            RunRetention.objects.get(run=self.run).reporting_acknowledged_at
        )

    def test_old_claim_and_old_ack_are_fenced_after_recovery(self):
        self.recover()
        with self.assertRaises(RunConflict):
            update_status(
                runner=self.runner,
                run_id=self.run.pk,
                claim_token=self.claim,
                data={"sequence": 100, "stage": "delivery"},
            )
        with self.assertRaises(RunConflict):
            acknowledge(
                self.runner,
                self.run.pk,
                self.claim,
                self.expected["expected_finish_digest"],
            )

    def test_recovery_blocks_new_agent_dispatch_on_server(self):
        run, recovery = self.recover()
        self.run, self.claim = run, recovery.claim_token
        from .native_services import validate_intent_binding

        value = copy.deepcopy(self.agent_intent.payload)
        value["attempt"] = 1
        with self.assertRaisesMessage(RunConflict, "cannot dispatch another agent"):
            validate_intent_binding(run, value, uuid4())

    def test_recovery_api_requires_controller_proof_and_returns_generation(self):
        response = self.post(
            "runner_delivery_recovery",
            {"operation_id": str(self.operation_id), **self.expected},
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["run"]["delivery_recovery"]["generation"], 1)
        self.assertEqual(
            response.json()["delivery_recovery"]["native"]["phase"], "recovery_delivery"
        )

    def test_changed_instructions_fence_recovery(self):
        from work_queue.models import QueueItem

        metadata = {**self.item.source_metadata, "body": "Changed source"}
        QueueItem.objects.filter(pk=self.item.pk).update(source_metadata=metadata)
        with self.assertRaises(RunConflict):
            self.recover()

    def test_changed_selected_policy_fences_recovery(self):
        from .settings_models import RunnerSettings

        settings = RunnerSettings.objects.get(pk=1)
        settings.execution_policy["repair_limit"] = 0
        settings.save()
        with self.assertRaises(RunConflict):
            self.recover()

    def test_recovery_history_cannot_be_modified_or_deleted(self):
        from django.core.exceptions import ValidationError

        _, recovery = self.recover()
        with self.assertRaises(ValidationError):
            recovery.save()
        with self.assertRaises(ValidationError):
            recovery.delete()
        with self.assertRaises(ValidationError):
            NativeDeliveryRecovery.objects.all().update(archive={})

    def test_recovery_rejects_unresolved_later_attempt(self):
        self.run.native_status.update(repair_count=1)
        self.run.save(update_fields=["native_status"])
        with self.assertRaises(RunConflict):
            self.recover()

    def test_recovery_preserves_monotonic_native_status_sequence(self):
        self.run.native_status.update(sequence=7)
        self.run.save(update_fields=["native_status"])
        run, recovery = self.recover()
        self.assertEqual(recovery.resume["status_sequence"], 7)
        with self.assertRaises(RunConflict):
            update_status(
                runner=self.runner,
                run_id=run.pk,
                claim_token=recovery.claim_token,
                data={"sequence": 7, "stage": "delivery"},
            )

    def test_recovery_cannot_read_backend_credentials(self):
        from django.core.exceptions import ValidationError
        from .secrets import secret_document

        run, recovery = self.recover()
        with self.assertRaises(ValidationError):
            secret_document(
                {
                    "scope": "run",
                    "run_id": str(run.pk),
                    "claim_token": str(recovery.claim_token),
                    "purpose": "implementation",
                },
                self.runner,
            )
