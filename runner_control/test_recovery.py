import json
from unittest.mock import patch
from uuid import uuid4

from django.test import TestCase
from django.urls import reverse

from projects.models import Project
from work_queue.models import QueueItem
from work_queue.services import enqueue_item, requeue_items

from .auth import token_digest
from .models import Run, RunnerIdentity
from .recovery_services import request_cancellation


class ControllerOwnershipTests(TestCase):
    def setUp(self):
        self.runner = RunnerIdentity.objects.create(
            name="recovery", token_digest=token_digest("runner-test")
        )
        self.client.defaults["HTTP_AUTHORIZATION"] = "Bearer runner-test"
        self.proof = "a" * 64
        self.project = Project.objects.create(
            name="Recovery", repository_url="https://github.com/example/repo"
        )
        self.item = enqueue_item(project=self.project, title="Recovery test")
        self.run_id = uuid4()
        snapshot = patch(
            "runner_control.services.locked_configuration_snapshot",
            return_value=("", {}),
        )
        snapshot.start()
        self.addCleanup(snapshot.stop)

    def post(self, name, body=None, run=False):
        path = reverse(name, args=[self.run_id] if run else [])
        return self.client.post(
            path, json.dumps(body or {}), content_type="application/json"
        )

    def bind(self):
        self.client.defaults["HTTP_X_NQ_CONTROLLER_SECRET"] = self.proof
        self.assertEqual(self.post("runner_controller").status_code, 200)

    def claim(self):
        response = self.post("runner_claim", {"run_id": str(self.run_id)})
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()["item"]["claim_token"]

    def test_unbound_identity_cannot_claim_or_request_secrets(self):
        self.assertEqual(
            self.post("runner_claim", {"run_id": str(self.run_id)}).status_code, 409
        )
        self.assertEqual(
            self.post("runner_selected_secret", {"scope": "preflight"}).status_code, 409
        )
        self.item.refresh_from_db()
        self.assertEqual(self.item.status, "queued")

    def test_volume_proof_is_not_serialized_and_same_volume_can_reconnect(self):
        self.bind()
        self.bind()
        token = self.claim()
        response = self.client.get(reverse("runner_detail", args=[self.run_id]))
        self.assertNotIn(self.proof, response.content.decode())
        self.assertNotIn(token, response.content.decode())

    def test_second_volume_cannot_bind_recover_or_finish(self):
        self.bind()
        token = self.claim()
        self.client.defaults["HTTP_X_NQ_CONTROLLER_SECRET"] = "b" * 64
        self.assertEqual(self.post("runner_controller").status_code, 409)
        self.assertEqual(self.post("runner_recover", run=True).status_code, 409)
        self.assertEqual(
            self.post(
                "runner_finish", {"claim_token": token, "status": "failed"}, run=True
            ).status_code,
            409,
        )

    def test_revoked_bearer_after_authentication_cannot_bind_volume(self):
        from .services import lock_controller

        self.client.defaults["HTTP_X_NQ_CONTROLLER_SECRET"] = self.proof

        def revoke_before_binding(runner):
            RunnerIdentity.objects.filter(pk=runner.pk).update(enabled=False)
            lock_controller(runner)

        with patch(
            "runner_control.recovery_api.lock_controller",
            side_effect=revoke_before_binding,
        ):
            response = self.post("runner_controller")
        self.assertEqual(response.status_code, 409)
        self.runner.refresh_from_db()
        self.assertEqual(self.runner.controller_digest, "")

    def test_volume_proof_revoked_after_authentication_cannot_claim(self):
        from .services import RunConflict, claim_run

        self.bind()
        self.runner.refresh_from_db()
        self.runner.controller_proven = True
        RunnerIdentity.objects.filter(pk=self.runner.pk).update(
            controller_digest=token_digest("b" * 64)
        )
        with self.assertRaises(RunConflict):
            claim_run(runner=self.runner, run_id=self.run_id)
        self.item.refresh_from_db()
        self.assertEqual(self.item.status, "queued")
        self.assertFalse(Run.objects.filter(pk=self.run_id).exists())

    def test_unbound_legacy_active_run_cannot_be_adopted(self):
        self.bind()
        self.claim()
        RunnerIdentity.objects.filter(pk=self.runner.pk).update(controller_digest="")
        self.assertEqual(self.post("runner_controller").status_code, 409)

    def test_admin_requeue_invalidates_old_owner(self):
        self.bind()
        token = self.claim()
        requeue_items(QueueItem.objects.filter(pk=self.item.pk))
        response = self.post(
            "runner_finish",
            {"claim_token": token, "status": "failed", "error_summary": "old"},
            run=True,
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(Run.objects.get(pk=self.run_id).state, "obsolete")

    def test_cancellation_requires_request_then_confirmed_termination(self):
        self.bind()
        token = self.claim()
        body = {"claim_token": token, "termination_confirmed": True}
        self.assertEqual(
            self.post("runner_cancellation", body, run=True).status_code, 409
        )
        run = request_cancellation(self.run_id, "Stop for operator")
        self.assertIsNone(run.cancellation_confirmed_at)
        failed = {
            "claim_token": token,
            "status": "failed",
            "error_summary": "Cancelled",
        }
        self.assertEqual(self.post("runner_finish", failed, run=True).status_code, 409)
        self.assertEqual(
            self.post("runner_cancellation", body, run=True).status_code, 200
        )
        self.assertEqual(self.post("runner_finish", failed, run=True).status_code, 200)
        self.item.refresh_from_db()
        self.assertEqual(self.item.status, "failed")

    def test_cancel_request_is_durable_and_idempotent(self):
        self.bind()
        self.claim()
        first = request_cancellation(self.run_id, "Stop")
        second = request_cancellation(self.run_id, "Changed note")
        self.assertEqual(
            first.cancellation_requested_at, second.cancellation_requested_at
        )
        self.assertEqual(second.cancellation_reason, "Stop")

    def test_cancellation_fences_stage_writes_before_confirmation(self):
        self.bind()
        token = self.claim()
        request_cancellation(self.run_id)
        payload = {
            "claim_token": token,
            "operation_id": str(uuid4()),
            "kind": "log",
            "data": {},
        }
        self.assertEqual(
            self.post("runner_artifact", payload, run=True).status_code, 409
        )


class OperationLookupTests(TestCase):
    def setUp(self):
        from .test_plans import ContextPlanAPITests

        ContextPlanAPITests.setUp(self)

    def context(self):
        from coding_runner.context import manual_context
        from coding_runner.test_plans import BASE
        from .test_plans import ContextPlanAPITests

        return ContextPlanAPITests.post(
            self,
            "runner_contexts",
            {
                "document": manual_context(self.claimed),
                "base_commit": BASE,
                "resolved_base_branch": "main",
                "reason": "Inspect",
            },
        )

    def lookup(self, operation_id):
        return self.client.get(
            reverse("runner_operation", args=[self.run_id, operation_id])
        )

    def test_context_operation_recovers_exact_immutable_response(self):
        response = self.context()
        context = response.json()["context"]
        recovered = self.lookup(context["operation_id"])
        self.assertEqual(recovered.status_code, 200)
        self.assertEqual(recovered.json()["response"], response.json())

    def test_plan_operation_recovers_exact_response(self):
        from coding_runner.test_plans import proposed_plan
        from .test_plans import ContextPlanAPITests

        context = self.context().json()["context"]
        response = ContextPlanAPITests.post(
            self,
            "runner_plans",
            {"document": proposed_plan(context), "reason": "Save plan"},
        )
        plan = response.json()["plan"]
        self.assertEqual(
            self.lookup(plan["operation_id"]).json()["response"], response.json()
        )

    def test_unknown_operation_is_proven_absent(self):
        self.assertEqual(self.lookup(uuid4()).status_code, 404)

    def test_operation_lookup_requires_volume_proof(self):
        self.client.defaults.pop("HTTP_X_NQ_CONTROLLER_SECRET")
        self.assertEqual(self.lookup(uuid4()).status_code, 409)
