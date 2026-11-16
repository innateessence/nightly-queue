import json
import uuid
from datetime import timedelta
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

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
            name="contexts", token_digest=token_digest(self.token)
        )
        self.client.defaults["HTTP_AUTHORIZATION"] = f"Bearer {self.token}"
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
            name="foreign", token_digest=token_digest("foreign-token")
        )
        self.client.defaults["HTTP_AUTHORIZATION"] = "Bearer foreign-token"
        for name in ["runner_contexts", "runner_plans"]:
            self.assertEqual(
                self.client.get(reverse(name, args=[self.run_id])).status_code, 404
            )

    def test_claim_and_bearer_values_are_redacted_before_saving(self):
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
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()["plan"]["document"]["findings"],
            ["[redacted]", "[redacted]"],
        )

    def test_saved_plan_view_displays_history_and_escapes_untrusted_text(self):
        context = self.save_context().json()["context"]
        plan = proposed_plan(context)
        plan["findings"] = ["<script>alert('issue')</script>"]
        self.save_plan(context, document=plan)
        response = self.client.get(reverse("runner_plan_detail", args=[self.run_id]))
        self.assertContains(response, "PLAN VERSION 1")
        self.assertContains(response, "&lt;script&gt;")
        self.assertNotContains(response, "<script>alert('issue')</script>")
        self.assertContains(response, "human approval")

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
