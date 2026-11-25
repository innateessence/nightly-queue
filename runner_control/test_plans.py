import copy
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.db import models
from django.test import TestCase, override_settings
from django.urls import reverse

from credentials.test_models import TEST_KEY

from .models import ContextSnapshot, PlanVersion
from .test_native_protocol import NativeProtocolMixin


@override_settings(VIBEARR_CREDENTIAL_KEY=TEST_KEY)
class ContextPlanAPITests(NativeProtocolMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.prepare_context()
        self.plan = self.run.plans.get()

    def context_payload(self):
        return {
            "claim_token": str(self.claim),
            "operation_id": str(self.context.operation_id),
            "document": self.context.document,
            "base_commit": self.context.inspected_base_commit,
            "resolved_base_branch": self.context.resolved_base_branch,
            "reason": self.context.reason,
        }

    def test_context_detail_returns_saved_native_document(self):
        response = self.client.get(
            reverse("runner_context_detail", args=[self.run.pk, self.context.pk])
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["context"]["document"], self.context.document)

    def test_plan_detail_returns_saved_native_document(self):
        response = self.client.get(
            reverse("runner_plan_document", args=[self.run.pk, self.plan.pk])
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["plan"]["document"], self.plan.document)

    def test_context_listing_omits_document_payloads(self):
        response = self.client.get(reverse("runner_contexts", args=[self.run.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("document", response.json()["contexts"][0])

    def test_plan_listing_omits_document_payloads(self):
        response = self.client.get(reverse("runner_plans", args=[self.run.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("document", response.json()["plans"][0])

    def test_context_operation_retry_reuses_immutable_version(self):
        response = self.post("runner_contexts", self.context_payload())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["context"]["id"], str(self.context.pk))
        self.assertEqual(self.run.contexts.count(), 1)

    def test_context_operation_cannot_change_content(self):
        payload = self.context_payload()
        payload["reason"] = "Different reason"
        self.assertEqual(self.post("runner_contexts", payload).status_code, 409)

    def test_context_operation_rejects_stale_claim(self):
        payload = self.context_payload()
        payload["claim_token"] = str(uuid4())
        self.assertEqual(self.post("runner_contexts", payload).status_code, 409)

    def test_context_rejects_selected_credential_material(self):
        payload = self.context_payload()
        payload["operation_id"] = str(uuid4())
        payload["document"] = copy.deepcopy(payload["document"])
        payload["document"]["acceptance_criteria"] = ["github-native-secret"]
        self.assertEqual(self.post("runner_contexts", payload).status_code, 400)

    def test_context_rejects_unknown_request_fields(self):
        payload = self.context_payload()
        payload["unexpected"] = "value"
        self.assertEqual(self.post("runner_contexts", payload).status_code, 400)

    def test_context_rejects_changed_inspected_base(self):
        payload = self.context_payload()
        payload["operation_id"] = str(uuid4())
        payload["base_commit"] = "f" * 40
        self.assertEqual(self.post("runner_contexts", payload).status_code, 409)

    def test_context_cursor_rejects_negative_values(self):
        response = self.client.get(
            reverse("runner_contexts", args=[self.run.pk]), {"after": -1}
        )
        self.assertEqual(response.status_code, 400)

    def test_context_detail_withholds_a_new_bearer_secret(self):
        document = copy.deepcopy(self.context.document)
        document["acceptance_criteria"] = [self.runner_token]
        models.QuerySet.update(
            ContextSnapshot.objects.filter(pk=self.context.pk), document=document
        )
        response = self.client.get(
            reverse("runner_context_detail", args=[self.run.pk, self.context.pk])
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(self.runner_token, response.content.decode())
        self.assertTrue(response.json()["context"]["presentation_only"])

    def test_document_querysets_cannot_mutate_saved_versions(self):
        with self.assertRaises(ValidationError):
            ContextSnapshot.objects.filter(pk=self.context.pk).update(reason="replace")
        with self.assertRaises(ValidationError):
            PlanVersion.objects.filter(pk=self.plan.pk).delete()

    def test_run_detail_displays_native_plan_and_context(self):
        response = self.client.get(reverse("runner_plan_detail", args=[self.run.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Frozen task and validation")
        self.assertContains(response, self.plan.document["problem"])
