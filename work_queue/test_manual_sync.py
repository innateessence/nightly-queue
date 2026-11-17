from unittest.mock import call, patch

from celery.result import EagerResult
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from kombu.exceptions import OperationalError

from credentials.models import APISettings, Credential
from credentials.test_models import TEST_KEY
from projects.models import Project

from .github import GitHubAPIError
from .models import QueueItem


@override_settings(NQ_CREDENTIAL_KEY=TEST_KEY, CELERY_TASK_ALWAYS_EAGER=True)
class ManualSyncTests(TestCase):
    def setUp(self):
        metadata = patch(
            "work_queue.tasks.repository_metadata",
            side_effect=lambda url, token, identity=None: dict(
                repository_id=99, owner_id=42, repository_url=url, default_branch="main"
            ),
        )
        metadata.start()
        self.addCleanup(metadata.stop)
        self.url = reverse("sync_issues")
        self.project = Project.objects.create(
            name="Auto",
            repository_url="https://github.com/example/auto",
            autonomous=True,
        )
        credential = Credential(name="Sync access", provider="github", kind="token")
        credential.set_secret("private-sync-token")
        credential.save()
        APISettings.objects.create(github_credential=credential)
        issue = {
            "number": 1,
            "title": "Imported issue",
            "body": "Context",
            "state": "open",
        }
        fetch = patch("work_queue.tasks.repository_issues", return_value=[issue])
        self.fetch = fetch.start()
        self.addCleanup(fetch.stop)

    def test_button_is_available_on_empty_queue(self):
        response = self.client.get(reverse("index"))
        self.assertContains(response, f'action="{self.url}"')
        self.assertContains(response, "Sync issues now")
        self.fetch.assert_not_called()

    def test_post_enqueues_issues_from_every_autonomous_project(self):
        second = Project.objects.create(
            name="Second",
            repository_url="https://github.com/example/second",
            autonomous=True,
        )
        Project.objects.create(
            name="Manual", repository_url="https://github.com/example/manual"
        )
        client = Client(enforce_csrf_checks=True)
        client.get(reverse("index"))
        token = client.cookies["csrftoken"].value
        response = client.post(self.url, HTTP_X_CSRFTOKEN=token, follow=True)
        self.assertRedirects(response, reverse("index"))
        self.fetch.assert_has_calls(
            [
                call(self.project.repository_url, "private-sync-token", etags={}),
                call(second.repository_url, "private-sync-token", etags={}),
            ]
        )
        self.assertEqual(self.fetch.call_count, 2)
        self.assertEqual(QueueItem.objects.filter(source="github").count(), 2)
        self.assertContains(response, "Projects synced: 2. New items: 2.")

    def test_repeated_click_does_not_duplicate_work(self):
        self.client.post(self.url)
        response = self.client.post(self.url, follow=True)
        self.assertEqual(QueueItem.objects.count(), 1)
        self.assertContains(response, "New items: 0.")

    def test_button_sync_removes_closed_issue_from_queued_work(self):
        self.client.post(self.url)
        self.fetch.return_value = [
            {
                "number": 1,
                "title": "Imported issue",
                "body": "Context",
                "state": "closed",
            }
        ]
        self.client.post(self.url)
        response = self.client.get(reverse("index"), {"status": "queued"})
        self.assertEqual(response.context["items"], [])
        self.assertEqual(response.context["queue_total"], 0)
        self.assertEqual(QueueItem.objects.get().status, QueueItem.Status.CLOSED)

    def test_button_syncs_immediately_during_scheduled_interval(self):
        last_sync = timezone.now()
        APISettings.objects.update(
            auto_sync_interval_hours=24, last_auto_sync_at=last_sync
        )
        response = self.client.post(self.url, follow=True)
        self.assertContains(response, "Projects synced: 1. New items: 1.")
        self.assertEqual(APISettings.objects.get().last_auto_sync_at, last_sync)

    def test_missing_selection_explains_how_to_enable_sync(self):
        APISettings.objects.all().delete()
        response = self.client.post(self.url, follow=True)
        self.assertContains(response, "Check the GitHub API credential in Settings")
        self.fetch.assert_not_called()

    def test_partial_failure_reports_failed_projects(self):
        Project.objects.create(
            name="Second",
            repository_url="https://github.com/example/second",
            autonomous=True,
        )
        self.fetch.side_effect = [GitHubAPIError("access denied"), []]
        with self.assertLogs("work_queue.tasks", level="WARNING"):
            response = self.client.post(self.url, follow=True)
        self.assertContains(response, "Projects that could not be synced: 1.")
        self.assertContains(response, "Projects synced: 1.")

    @patch("work_queue.views.import_auto_issues.delay")
    def test_background_dispatch_reports_started_without_waiting(self, dispatch):
        response = self.client.post(self.url, follow=True)
        dispatch.assert_called_once_with()
        self.assertContains(response, "Issue sync started for all autonomous projects.")
        self.fetch.assert_not_called()

    @patch("work_queue.views.import_auto_issues.delay")
    def test_dispatch_failure_shows_safe_error(self, dispatch):
        dispatch.side_effect = OperationalError("private-connection-details")
        response = self.client.post(self.url, follow=True)
        self.assertContains(response, "Unable to start issue sync.")
        self.assertNotContains(response, "private-connection-details")

    @patch("work_queue.views.import_auto_issues.delay")
    def test_eager_failure_shows_safe_error(self, dispatch):
        dispatch.return_value = EagerResult(
            "sync-id", ValueError("private-sync-token"), "FAILURE"
        )
        response = self.client.post(self.url, follow=True)
        self.assertContains(response, "Issue sync failed.")
        self.assertNotContains(response, "private-sync-token")

    @patch("work_queue.views.import_auto_issues.delay")
    def test_get_does_not_start_sync(self, dispatch):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 405)
        dispatch.assert_not_called()

    @patch("work_queue.views.import_auto_issues.delay")
    def test_post_requires_csrf(self, dispatch):
        response = Client(enforce_csrf_checks=True).post(self.url)
        self.assertEqual(response.status_code, 403)
        dispatch.assert_not_called()
