from .test_support import CONTROLLER_SECRET, provision_controller
import json
import uuid
from datetime import timedelta
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from credentials.models import Credential
from credentials.test_models import TEST_KEY

from coding_runner.context import manual_context
from coding_runner.test_plans import BASE, proposed_plan
from projects.models import Project
from work_queue.services import enqueue_item, requeue_items

from .auth import token_digest
from .models import ContextSnapshot, PlanVersion, Run, RunnerIdentity


class ContextPlanAPITests(TestCase):
    def setUp(self):
        self.token = "test-runner-context-token"
        self.runner = RunnerIdentity.objects.create(
            controller_digest=token_digest(CONTROLLER_SECRET),
            name="contexts",
            token_digest=token_digest(self.token),
        )
        self.client.defaults["HTTP_AUTHORIZATION"] = f"Bearer {self.token}"
        provision_controller(self)
        self.project = Project.objects.create(
            name="Example",
            repository_url="https://github.com/example/repo",
            base_branch="main",
        )
        self.item = enqueue_item(
            project=self.project,
            title="Improve retries",
            instructions="Preserve saved evidence",
        )
        self.run_id = uuid.uuid4()
        with patch(
            "runner_control.services.locked_configuration_snapshot",
            return_value=("", {}),
        ):
            response = self.client.post(
                reverse("runner_claim"),
                json.dumps({"run_id": str(self.run_id)}),
                content_type="application/json",
            )
        self.assertEqual(response.status_code, 200)
        self.claimed = response.json()["item"]
        self.claim_token = self.claimed["claim_token"]

    def post(self, name, data, *, run_id=None):
        payload = {
            "claim_token": self.claim_token,
            "operation_id": str(uuid.uuid4()),
            **data,
        }
        return self.client.post(
            reverse(name, args=[run_id or self.run_id]),
            json.dumps(payload),
            content_type="application/json",
        )

    def save_context(
        self,
        *,
        base=BASE,
        reason="Inspect current task",
        document=None,
        operation_id=None,
    ):
        fields = {
            "document": document or manual_context(self.claimed),
            "base_commit": base,
            "resolved_base_branch": "main",
            "reason": reason,
        }
        if operation_id:
            fields["operation_id"] = operation_id
        return self.post("runner_contexts", fields)

    def save_plan(
        self,
        context=None,
        *,
        blockers=None,
        reason="Initial implementation plan",
        document=None,
    ):
        context = context or self.save_context().json()["context"]
        document = document or proposed_plan(context)
        if blockers:
            document["blockers"] = blockers
        return self.post("runner_plans", {"document": document, "reason": reason})

    def handoff(self, context):
        return self.post(
            "runner_handoff",
            {
                "context_id": context["id"],
                "base_commit": context["inspected_base_commit"],
            },
        )

    def valid_handoff(self):
        context = self.save_context().json()["context"]
        plan = self.save_plan(context).json()["plan"]
        fresh = self.save_context(reason="Refresh before implementation").json()[
            "context"
        ]
        self.assertEqual(self.handoff(fresh).status_code, 200)
        return plan, fresh

    def test_manual_context_preserves_claimed_instructions_without_issue(self):
        response = self.save_context()
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.json()["context"]["document"]["issue"])

    def test_changed_project_url_does_not_change_frozen_checkout(self):
        self.project.repository_url = "https://github.com/other/project"
        self.project.save()
        self.assertEqual(self.save_context().status_code, 200)
        document = manual_context(self.claimed)
        document["checkout"]["repository_url"] = self.project.repository_url
        self.assertEqual(self.save_context(document=document).status_code, 400)

    def test_duplicate_context_operation_is_idempotent(self):
        operation = str(uuid.uuid4())
        document = manual_context(self.claimed)
        first = self.save_context(operation_id=operation, document=document)
        second = self.save_context(operation_id=operation, document=document)
        self.assertEqual(first.json(), second.json())
        self.assertEqual(ContextSnapshot.objects.count(), 1)

    def test_duplicate_operation_with_different_base_conflicts(self):
        operation = str(uuid.uuid4())
        document = manual_context(self.claimed)
        self.save_context(operation_id=operation, document=document)
        self.assertEqual(
            self.save_context(
                operation_id=operation, document=document, base="d" * 40
            ).status_code,
            409,
        )

    def test_context_and_plan_versions_cannot_be_mutated(self):
        self.valid_handoff()
        for entry in [ContextSnapshot.objects.first(), PlanVersion.objects.first()]:
            with self.assertRaises(ValidationError):
                entry.save()
            with self.assertRaises(ValidationError):
                entry.delete()

    def test_queryset_cannot_rewrite_or_delete_saved_versions(self):
        self.valid_handoff()
        for model in [ContextSnapshot, PlanVersion]:
            with self.assertRaises(ValidationError):
                model.objects.all().update(reason="Changed later")
            with self.assertRaises(ValidationError):
                model.objects.all().delete()

    def test_plan_requires_a_saved_complete_context(self):
        self.assertEqual(
            self.post(
                "runner_plans", {"document": {}, "reason": "No context"}
            ).status_code,
            409,
        )

    def test_missing_plan_fields_are_rejected(self):
        context = self.save_context().json()["context"]
        document = proposed_plan(context)
        del document["steps"]
        self.assertEqual(self.save_plan(context, document=document).status_code, 400)
        self.assertFalse(PlanVersion.objects.exists())

    def test_implementation_requires_saved_plan(self):
        response = self.post("runner_progress", {"stage": "implementation"})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(Run.objects.get(pk=self.run_id).stage, "claimed")

    def test_handoff_requires_a_fresh_post_plan_discussion_read(self):
        context = self.save_context().json()["context"]
        self.save_plan(context)
        response = self.handoff(context)
        self.assertEqual(response.status_code, 409)
        self.assertIn("Refresh", response.json()["error"])

    def test_unresolved_blockers_prevent_handoff(self):
        context = self.save_context().json()["context"]
        self.save_plan(context, blockers=["Conflicting requirements need a decision"])
        fresh = self.save_context().json()["context"]
        response = self.handoff(fresh)
        self.assertEqual(response.status_code, 409)
        self.assertIn("blockers", response.json()["error"])

    def test_valid_plan_and_refreshed_context_allow_implementation(self):
        plan, _ = self.valid_handoff()
        response = self.post("runner_progress", {"stage": "implementation"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Run.objects.get(pk=self.run_id).plan_reference, plan["id"])

    def test_changed_base_blocks_implementation_until_plan_revision(self):
        self.valid_handoff()
        refreshed = self.save_context(base="d" * 40, reason="Upstream changed").json()[
            "context"
        ]
        self.assertEqual(refreshed["changes"], ["base_commit"])
        self.assertEqual(self.handoff(refreshed).status_code, 409)
        revised = self.save_plan(
            refreshed, reason="Inspected upstream and revised validation"
        ).json()["plan"]
        current = self.save_context(base="d" * 40).json()["context"]
        self.assertEqual(self.handoff(current).status_code, 200)
        self.assertEqual(revised["version"], 2)
        self.assertIsNotNone(revised["supersedes"])
        self.assertEqual(PlanVersion.objects.count(), 2)

    def test_revision_reason_is_required_and_prior_plan_retained(self):
        context = self.save_context().json()["context"]
        self.save_plan(context)
        self.assertEqual(self.save_plan(context, reason="").status_code, 400)
        self.assertEqual(PlanVersion.objects.count(), 1)

    def test_stale_claim_cannot_save_context(self):
        self.item.refresh_from_db()
        requeue_items(type(self.item).objects.filter(pk=self.item.pk))
        self.assertEqual(self.save_context().status_code, 409)

    def test_foreign_runner_cannot_read_context_or_plans(self):
        self.valid_handoff()
        RunnerIdentity.objects.create(
            controller_digest=token_digest(CONTROLLER_SECRET),
            name="foreign",
            token_digest=token_digest("foreign-token"),
        )
        self.client.defaults["HTTP_AUTHORIZATION"] = "Bearer foreign-token"
        for name in ["runner_contexts", "runner_plans"]:
            self.assertEqual(
                self.client.get(reverse(name, args=[self.run_id])).status_code, 404
            )

    def test_claim_and_bearer_values_are_rejected_without_mutating_provenance(self):
        self.claimed["instructions"] = (
            ""  # Use plan text so claimed manual instructions stay fixed.
        )
        context = self.save_context(
            document=manual_context(
                {**self.claimed, "instructions": "Preserve saved evidence"}
            )
        ).json()["context"]
        plan = proposed_plan(context)
        plan["findings"] = [self.claim_token, self.token]
        response = self.save_plan(context, document=plan)
        self.assertEqual(response.status_code, 400)
        self.assertNotContains(response, self.token, status_code=400)
        self.assertNotContains(response, self.claim_token, status_code=400)
        self.assertFalse(PlanVersion.objects.exists())

    def test_saved_plan_view_displays_history_and_escapes_untrusted_text(self):
        context = self.save_context().json()["context"]
        plan = proposed_plan(context)
        plan["findings"] = ["<script>alert('issue')</script>"]
        self.save_plan(context, document=plan)
        response = self.client.get(reverse("runner_plan_detail", args=[self.run_id]))
        self.assertContains(response, "PLAN VERSION 1")
        self.assertContains(response, "&lt;script&gt;")
        self.assertNotContains(response, "<script>alert('issue')</script>")
        self.assertContains(response, "Legacy proposal execution is retired")

    def test_expired_refresh_cannot_start_implementation(self):
        self.valid_handoff()
        future = timezone.now() + timedelta(minutes=6)
        with patch("runner_control.plan_services.timezone.now", return_value=future):
            self.assertEqual(
                self.post("runner_progress", {"stage": "implementation"}).status_code,
                409,
            )

    def test_plan_write_replay_after_refresh_returns_immutable_original(self):
        context = self.save_context().json()["context"]
        operation = str(uuid.uuid4())
        payload = {
            "operation_id": operation,
            "document": proposed_plan(context),
            "reason": "Initial plan",
        }
        first = self.post("runner_plans", payload)
        self.save_context(base="d" * 40, reason="A later base refresh")
        second = self.post("runner_plans", payload)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.json(), second.json())
        self.assertEqual(PlanVersion.objects.count(), 1)

    def test_context_list_is_bounded_metadata_and_details_are_owned(self):
        for _ in range(12):
            self.assertEqual(self.save_context().status_code, 200)
        response = self.client.get(reverse("runner_contexts", args=[self.run_id]))
        page = response.json()
        self.assertEqual(len(page["contexts"]), 10)
        self.assertTrue(page["has_more"])
        self.assertEqual(page["next_after"], 10)
        self.assertNotIn("document", page["contexts"][0])
        second = self.client.get(
            reverse("runner_contexts", args=[self.run_id]), {"after": 10}
        ).json()
        self.assertEqual(len(second["contexts"]), 2)
        self.assertFalse(second["has_more"])
        detail = self.client.get(
            reverse(
                "runner_context_detail", args=[self.run_id, page["contexts"][0]["id"]]
            )
        )
        self.assertEqual(detail.status_code, 200)
        self.assertIn("document", detail.json()["context"])

    def test_different_run_cannot_read_a_context_document(self):
        context = self.save_context().json()["context"]
        response = self.client.get(
            reverse("runner_context_detail", args=[uuid.uuid4(), context["id"]])
        )
        self.assertEqual(response.status_code, 404)

    def test_changed_discussion_requires_an_explicit_revised_plan(self):
        from coding_runner.test_context import comment, issue
        from coding_runner.context import GitHubContextClient

        self.item.refresh_from_db()
        self.item.source = "github"
        self.item.issue_repository = "https://github.com/original/issues.git"
        self.item.issue_number = 7
        self.item.save()
        self.claimed.update(
            source="github", issue_repository=self.item.issue_repository, issue_number=7
        )
        reader = GitHubContextClient("selected-read-token")
        with patch.object(reader, "_get", side_effect=[(issue(), ""), ([], "")]):
            initial = reader.fetch(self.claimed)
        context = self.save_context(document=initial).json()["context"]
        self.save_plan(context)
        with patch.object(
            reader, "_get", side_effect=[(issue(comments=1), ""), ([comment()], "")]
        ):
            changed = reader.fetch(self.claimed)
        refreshed = self.save_context(
            document=changed, reason="Issue discussion changed"
        ).json()["context"]
        self.assertEqual(refreshed["changes"], ["discussion"])
        self.assertEqual(self.handoff(refreshed).status_code, 409)
        revised = self.save_plan(
            refreshed, reason="Assessed new comment and added regression case"
        )
        self.assertEqual(revised.status_code, 200)
        changed["fetched_at"] = timezone.now().isoformat()
        fresh = self.save_context(document=changed).json()["context"]
        self.assertEqual(self.handoff(fresh).status_code, 200)

    def test_closed_issue_snapshot_cannot_be_saved(self):
        self.item.refresh_from_db()
        self.item.source = "github"
        self.item.issue_repository = "https://github.com/original/issues.git"
        self.item.issue_number = 7
        self.item.save()
        self.claimed.update(
            source="github", issue_repository=self.item.issue_repository, issue_number=7
        )
        from coding_runner.context import GitHubContextClient
        from coding_runner.test_context import issue

        reader = GitHubContextClient("selected-read-token")
        with patch.object(reader, "_get", side_effect=[(issue(), ""), ([], "")]):
            document = reader.fetch(self.claimed)
        document["issue"]["state"] = "closed"
        self.assertEqual(self.save_context(document=document).status_code, 400)
        self.assertFalse(ContextSnapshot.objects.exists())

    def test_stale_actual_fetch_cannot_be_reuploaded_as_a_fresh_handoff(self):
        context = self.save_context().json()["context"]
        self.save_plan(context)
        stale = manual_context(self.claimed)
        stale["fetched_at"] = (timezone.now() - timedelta(days=1)).isoformat()
        refreshed = self.save_context(document=stale).json()["context"]
        self.assertEqual(self.handoff(refreshed).status_code, 409)

    def test_future_fetch_timestamp_cannot_authorize_handoff(self):
        context = self.save_context().json()["context"]
        self.save_plan(context)
        future = manual_context(self.claimed)
        future["fetched_at"] = (timezone.now() + timedelta(days=1)).isoformat()
        refreshed = self.save_context(document=future).json()["context"]
        self.assertEqual(self.handoff(refreshed).status_code, 409)

    def test_handoff_operation_reconciles_after_a_new_context_and_expiry(self):
        context = self.save_context().json()["context"]
        self.save_plan(context)
        fresh = self.save_context().json()["context"]
        operation = str(uuid.uuid4())
        payload = {
            "operation_id": operation,
            "context_id": fresh["id"],
            "base_commit": BASE,
        }
        first = self.post("runner_handoff", payload)
        self.assertEqual(first.status_code, 200)
        self.save_context()
        future = timezone.now() + timedelta(minutes=6)
        with patch("runner_control.plan_services.timezone.now", return_value=future):
            replay = self.post("runner_handoff", payload)
            self.assertEqual(replay.json(), first.json())
            self.assertEqual(
                self.post("runner_progress", {"stage": "implementation"}).status_code,
                409,
            )
        reconciled = self.client.get(
            reverse("runner_handoff_detail", args=[self.run_id, operation])
        )
        self.assertEqual(reconciled.json(), first.json())
        self.assertEqual(
            Run.objects.get(pk=self.run_id)
            .evidence.filter(kind="plan_handoff")
            .count(),
            1,
        )

    def test_handoff_reconciliation_is_owned_and_unknown_operations_are_missing(self):
        self.assertEqual(
            self.client.get(
                reverse("runner_handoff_detail", args=[self.run_id, uuid.uuid4()])
            ).status_code,
            404,
        )
        self.valid_handoff()
        operation = (
            Run.objects.get(pk=self.run_id)
            .evidence.get(kind="plan_handoff")
            .operation_id
        )
        RunnerIdentity.objects.create(
            name="other-reader",
            token_digest=token_digest("other-reader-token"),
            controller_digest=token_digest(CONTROLLER_SECRET),
        )
        self.client.defaults["HTTP_AUTHORIZATION"] = "Bearer other-reader-token"
        self.assertEqual(
            self.client.get(
                reverse("runner_handoff_detail", args=[self.run_id, operation])
            ).status_code,
            404,
        )

    def test_generic_artifacts_cannot_forge_a_handoff(self):
        response = self.post("runner_artifact", {"kind": "plan_handoff", "data": {}})
        self.assertEqual(response.status_code, 400)

    def test_nonfinite_replayed_document_is_a_validation_error(self):
        operation = str(uuid.uuid4())
        self.save_context(operation_id=operation)
        response = self.save_context(
            operation_id=operation, document={"invalid": float("nan")}
        )
        self.assertEqual(response.status_code, 400)

    def test_detail_paginates_saved_documents_with_separate_cursors(self):
        for index in range(3):
            context = self.save_context().json()["context"]
            self.save_plan(context, reason=f"Assessment {index}")
        response = self.client.get(reverse("runner_plan_detail", args=[self.run_id]))
        self.assertEqual(len(response.context["contexts"]), 1)
        self.assertEqual(len(response.context["plans"]), 1)
        self.assertContains(response, "PLAN VERSION 3")
        self.assertNotContains(response, "PLAN VERSION 2")
        older = self.client.get(
            reverse("runner_plan_detail", args=[self.run_id]),
            {"context_page": 2, "plan_page": 2},
        )
        self.assertContains(older, "PLAN VERSION 2")
        self.assertContains(older, "CONTEXT SNAPSHOT 2")
        self.assertContains(older, "Older context")

    def test_history_pages_keep_older_runs_accessible(self):
        for _ in range(100):
            Run.objects.create(runner=self.runner, state=Run.State.EMPTY)
        response = self.client.get(reverse("runner_plan_history"))
        self.assertEqual(len(response.context["runs"]), 25)
        self.assertContains(response, "Older runs")
        last = self.client.get(reverse("runner_plan_history"), {"page": 5})
        self.assertContains(last, str(self.run_id))

    def select_rotated_credential(self):
        credential = Credential(
            name="Context review backend", provider="codex", kind="token"
        )
        credential.set_secret("historic-context-secret")
        credential.save()
        credential.set_secret("current-context-secret")
        credential.save()
        run = Run.objects.get(pk=self.run_id)
        run.configuration_snapshot = {
            "effective": {"research_credential_id": credential.pk}
        }
        run.save(update_fields=["configuration_snapshot"])
        return credential

    @override_settings(NQ_CREDENTIAL_KEY=TEST_KEY)
    def test_selected_current_and_historical_secrets_are_rejected_in_actual_plans(self):
        context = self.save_context().json()["context"]
        self.select_rotated_credential()
        for material in ["historic-context-secret", "current-context-secret"]:
            document = proposed_plan(context)
            document["findings"] = [material]
            response = self.save_plan(context, document=document)
            self.assertEqual(response.status_code, 400)
            self.assertNotContains(response, material, status_code=400)
        self.assertFalse(PlanVersion.objects.exists())

    @override_settings(NQ_CREDENTIAL_KEY=TEST_KEY)
    def test_context_with_selected_material_is_rejected_without_mutation(self):
        self.item.instructions = "historic-context-secret"
        self.item.save(update_fields=["instructions"])
        self.claimed["instructions"] = self.item.instructions
        self.select_rotated_credential()
        response = self.save_context()
        self.assertEqual(response.status_code, 400)
        self.assertFalse(ContextSnapshot.objects.exists())
        self.item.refresh_from_db()
        self.assertEqual(self.item.instructions, "historic-context-secret")

    @override_settings(NQ_CREDENTIAL_KEY=TEST_KEY)
    def test_selected_secrets_are_redacted_from_persisted_revision_reasons(self):
        self.select_rotated_credential()
        response = self.save_context(
            reason="historic-context-secret and current-context-secret"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()["context"]["reason"], "[redacted] and [redacted]"
        )
        self.assertEqual(
            ContextSnapshot.objects.get().reason, "[redacted] and [redacted]"
        )

    def historical_secret_documents(self):
        self.item.instructions = "historic-context-secret"
        self.item.save(update_fields=["instructions"])
        self.claimed["instructions"] = self.item.instructions
        context = self.save_context(reason="historic-context-secret").json()["context"]
        document = proposed_plan(context)
        document["findings"] = ["historic-context-secret", "current-context-secret"]
        plan = self.save_plan(
            context, document=document, reason="current-context-secret"
        ).json()["plan"]
        fresh = self.save_context().json()["context"]
        result = self.handoff(fresh)
        self.assertEqual(result.status_code, 200)
        self.select_rotated_credential()
        return context, plan, fresh

    @override_settings(NQ_CREDENTIAL_KEY=TEST_KEY)
    def test_historical_readbacks_scrub_selected_values_without_rewriting_versions(
        self,
    ):
        context, plan, fresh = self.historical_secret_documents()
        original_context = ContextSnapshot.objects.get(pk=context["id"])
        original_plan = PlanVersion.objects.get(pk=plan["id"])
        operation = (
            Run.objects.get(pk=self.run_id)
            .evidence.get(kind="plan_handoff")
            .operation_id
        )
        urls = [
            reverse("runner_context_detail", args=[self.run_id, context["id"]]),
            reverse("runner_plan_document", args=[self.run_id, plan["id"]]),
            reverse("runner_handoff_detail", args=[self.run_id, operation]),
            reverse("runner_contexts", args=[self.run_id]),
            reverse("runner_plans", args=[self.run_id]),
        ]
        for url in urls:
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200)
            self.assertNotContains(response, "historic-context-secret")
            self.assertNotContains(response, "current-context-secret")
        detail = self.client.get(urls[0]).json()["context"]
        self.assertTrue(detail["presentation_only"])
        self.assertEqual(detail["digest"], original_context.digest)
        original_context.refresh_from_db()
        original_plan.refresh_from_db()
        self.assertEqual(original_context.document, context["document"])
        self.assertEqual(original_plan.document, plan["document"])
        self.assertEqual(original_context.reason, "historic-context-secret")
        self.assertEqual(self.handoff(fresh).status_code, 400)
        self.assertEqual(
            self.post("runner_progress", {"stage": "implementation"}).status_code, 400
        )

    @override_settings(NQ_CREDENTIAL_KEY=TEST_KEY)
    def test_operator_history_and_document_pages_redact_item_titles_and_content(self):
        self.historical_secret_documents()
        self.item.title = "current-context-secret"
        self.item.save(update_fields=["title"])
        run = Run.objects.get(pk=self.run_id)
        run.wait_reason = "historic-context-secret"
        run.save(update_fields=["wait_reason"])
        for name, args in [
            ("runner_plan_history", []),
            ("runner_plan_detail", [self.run_id]),
        ]:
            response = self.client.get(reverse(name, args=args))
            self.assertEqual(response.status_code, 200)
            self.assertNotContains(response, "historic-context-secret")
            self.assertNotContains(response, "current-context-secret")
            self.assertEqual(response["Cache-Control"], "no-store")
        self.assertContains(response, "Redacted presentation only")

    @override_settings(NQ_CREDENTIAL_KEY=TEST_KEY)
    def test_unavailable_selected_key_hides_historical_documents_and_titles(self):
        context, plan, _ = self.historical_secret_documents()
        self.item.title = "current-context-secret"
        self.item.save(update_fields=["title"])
        urls = [
            reverse("runner_context_detail", args=[self.run_id, context["id"]]),
            reverse("runner_plan_document", args=[self.run_id, plan["id"]]),
            reverse("runner_plan_detail", args=[self.run_id]),
            reverse("runner_plan_history"),
        ]
        with override_settings(NQ_CREDENTIAL_KEY=""):
            for url in urls:
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)
                self.assertNotContains(response, "historic-context-secret")
                self.assertNotContains(response, "current-context-secret")
                self.assertContains(response, "unavailable")
            self.assertEqual(self.save_context().status_code, 400)
        self.assertEqual(
            ContextSnapshot.objects.get(pk=context["id"]).document, context["document"]
        )

    @override_settings(NQ_CREDENTIAL_KEY=TEST_KEY)
    def test_unselected_credential_is_not_decrypted_for_document_reporting(self):
        context = self.save_context().json()["context"]
        self.select_rotated_credential()
        unrelated = Credential(
            name="Unrelated invalid backend",
            provider="codex",
            kind="token",
            encrypted_payload="not-ciphertext",
        )
        unrelated.save()
        response = self.client.get(
            reverse("runner_context_detail", args=[self.run_id, context["id"]])
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["context"]["unavailable"])

    @override_settings(NQ_CREDENTIAL_KEY=TEST_KEY)
    def test_credential_matching_a_response_field_preserves_response_structure(self):
        credential = Credential(
            name="Response field credential", provider="codex", kind="token"
        )
        credential.set_secret("document")
        credential.save()
        run = Run.objects.get(pk=self.run_id)
        run.configuration_snapshot = {
            "effective": {"research_credential_id": credential.pk}
        }
        run.save(update_fields=["configuration_snapshot"])
        response = self.save_context()
        self.assertEqual(response.status_code, 200)
        context = response.json()["context"]
        self.assertIn("document", context)
        self.assertFalse(context["presentation_only"])
        self.assertEqual(context["document"], ContextSnapshot.objects.get().document)
