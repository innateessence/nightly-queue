from uuid import uuid4

from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from credentials.models import Credential
from credentials.test_models import TEST_KEY
from projects.models import Project
from work_queue.models import QueueItem
from .models import Run, RunEvidence, RunnerHealth, RunnerIdentity


class OperationsUITests(TestCase):
    @classmethod
    def setUpTestData(cls):
        project = Project.objects.create(
            name="UI project", repository_url="https://github.com/example/ui"
        )
        cls.item = QueueItem.objects.create(
            project=project, title="Implement queue filter", status="running"
        )
        cls.runner = RunnerIdentity.objects.create(
            name="ui-worker", token_digest="x" * 64
        )
        cls.attempt = Run.objects.create(
            runner=cls.runner,
            item=cls.item,
            state="active",
            active_slot=1,
            stage="validation",
            wait_reason="Waiting for test database",
            configuration_snapshot={"execution_kind": "native"},
            heartbeat_at=timezone.now(),
            native_status={
                "validation_status": "running",
                "repair_count": 1,
                "detail": "Checking candidate tree",
            },
        )

    def test_navigation_has_operations_pages_without_approval_inbox(self):
        response = self.client.get(reverse("index"))
        for name in ["runner_activity", "runner_plan_history", "runner_system"]:
            self.assertContains(response, f'href="{reverse(name)}"')

    def test_queue_links_to_latest_attempt(self):
        response = self.client.get(reverse("index"))
        self.assertContains(
            response, reverse("runner_plan_detail", args=[self.attempt.pk])
        )

    def test_activity_only_shows_active_runs_and_saved_progress(self):
        Run.objects.create(runner=self.runner, state="empty")
        response = self.client.get(reverse("runner_activity"))
        self.assertEqual(
            [run.pk for run in response.context["runs"]], [self.attempt.pk]
        )
        for text in [
            "Validation",
            "Waiting for test database",
            "Checking candidate tree",
            "Last heartbeat",
        ]:
            self.assertContains(response, text)
        self.assertEqual(response["Cache-Control"], "no-store")

    def test_activity_empty_state_links_to_queue_and_diagnostics(self):
        Run.objects.filter(pk=self.attempt.pk).update(
            state="completed", active_slot=None
        )
        response = self.client.get(reverse("runner_activity"))
        self.assertContains(response, "No active work")
        self.assertContains(response, reverse("runner_system"))

    def test_activity_escapes_task_and_log_content(self):
        QueueItem.objects.filter(pk=self.item.pk).update(
            title="<script>task()</script>"
        )
        Run.objects.filter(pk=self.attempt.pk).update(
            native_status={"detail": "<img src=x onerror=alert(1)>"}
        )
        response = self.client.get(reverse("runner_activity"))
        self.assertContains(response, "&lt;script&gt;")
        self.assertContains(response, "&lt;img")
        self.assertNotContains(response, "<img")

    @override_settings(VIBEARR_CREDENTIAL_KEY=TEST_KEY)
    def test_activity_redacts_selected_credentials_and_claim_tokens(self):
        credential = Credential(name="UI backend", provider="codex", kind="token")
        credential.set_secret("ui-selected-secret")
        credential.save()
        claim = uuid4()
        QueueItem.objects.filter(pk=self.item.pk).update(claim_token=claim)
        Run.objects.filter(pk=self.attempt.pk).update(
            configuration_snapshot={
                "effective": {"implementation_credential_id": credential.pk}
            },
            native_status={"detail": f"ui-selected-secret {claim}"},
        )
        for name in ["runner_activity", "runner_plan_history"]:
            response = self.client.get(reverse(name))
            self.assertNotContains(response, "ui-selected-secret")
            self.assertNotContains(response, str(claim))

    def test_history_preserves_selected_state_and_pagination(self):
        for _ in range(26):
            Run.objects.create(runner=self.runner, state="empty")
        response = self.client.get(reverse("runner_plan_history"), {"state": "empty"})
        self.assertContains(response, 'value="empty" selected')
        self.assertContains(response, "?page=2&amp;state=empty")
        self.assertEqual(len(response.context["runs"]), 25)

    def test_history_shows_verified_delivery_link_and_outcome(self):
        Run.objects.filter(pk=self.attempt.pk).update(
            state="completed",
            active_slot=None,
            result_status="succeeded",
            result_url="https://github.com/example/ui/pull/7",
            native_outcome="merged",
            native_status={"validation_status": "passed"},
        )
        response = self.client.get(reverse("runner_plan_history"))
        for text in ["merged", "passed", "https://github.com/example/ui/pull/7"]:
            self.assertContains(response, text)

    def test_system_missing_reports_are_unknown(self):
        response = self.client.get(reverse("runner_system"))
        self.assertContains(response, "Free space and worker health are unknown")
        self.assertContains(response, "No processing gate evidence")
        self.assertContains(response, "Disabled")
        self.assertEqual(response["Cache-Control"], "no-store")

    def test_system_health_escapes_untrusted_report_text(self):
        RunnerHealth.objects.create(
            runner=self.runner,
            report={"free_bytes": 4096, "blocking_reason": "<script>bad()</script>"},
        )
        response = self.client.get(reverse("runner_system"))
        self.assertEqual(
            response.context["health_reports"][0].report["free_bytes"], 4096
        )
        self.assertContains(response, "&lt;script&gt;")
        self.assertNotContains(response, "<script>bad()")

    def test_run_detail_exposes_existing_cancel_and_retry_forms(self):
        response = self.client.get(
            reverse("runner_plan_detail", args=[self.attempt.pk])
        )
        self.assertContains(
            response, reverse("runner_operator_cancel", args=[self.attempt.pk])
        )
        self.assertContains(
            response, reverse("runner_operator_retry", args=[self.attempt.pk])
        )
        self.assertContains(response, 'name="csrfmiddlewaretoken"')

    def test_operator_mutations_require_csrf(self):
        client = Client(enforce_csrf_checks=True)
        for name in ["runner_operator_cancel", "runner_operator_retry"]:
            response = client.post(reverse(name, args=[self.attempt.pk]))
            self.assertEqual(response.status_code, 403)

    def test_reading_operations_pages_does_not_change_runs_or_evidence(self):
        before = list(Run.objects.values())
        for name in ["runner_activity", "runner_plan_history", "runner_system"]:
            self.client.get(reverse(name))
        self.assertEqual(list(Run.objects.values()), before)
        self.assertEqual(RunEvidence.objects.count(), 0)

    def test_operations_pages_reject_posts(self):
        for name in ["runner_activity", "runner_plan_history", "runner_system"]:
            self.assertEqual(self.client.post(reverse(name)).status_code, 405)
