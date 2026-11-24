from unittest.mock import patch

from django.db import DatabaseError
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from credentials.models import APISettings, Credential
from credentials.test_models import TEST_KEY
from projects.models import Project

from .import_jobs import claim_import, run_import
from .models import ImportJob, QueueItem


class ManualSyncTests(TestCase):
    def test_button_is_available_on_empty_queue(self):
        response = self.client.get(reverse("index"))
        self.assertContains(response, "Sync issues now")

    def test_post_enqueues_durable_manual_job(self):
        response = self.client.post(reverse("sync_issues"), follow=True)
        self.assertContains(response, "Issue sync queued for all autonomous projects.")
        self.assertFalse(ImportJob.objects.get().scheduled)

    def test_repeated_click_does_not_duplicate_pending_job(self):
        self.client.post(reverse("sync_issues"))
        self.client.post(reverse("sync_issues"))
        self.assertEqual(ImportJob.objects.count(), 1)

    @patch("work_queue.views.enqueue_import", side_effect=DatabaseError("private"))
    def test_dispatch_failure_shows_safe_error(self, _enqueue):
        response = self.client.post(reverse("sync_issues"), follow=True)
        self.assertContains(response, "Unable to start issue sync.")
        self.assertNotContains(response, "private")

    def test_get_does_not_start_sync(self):
        response = self.client.get(reverse("sync_issues"))
        self.assertEqual(response.status_code, 405)
        self.assertFalse(ImportJob.objects.exists())

    def test_post_requires_csrf(self):
        response = Client(enforce_csrf_checks=True).post(reverse("sync_issues"))
        self.assertEqual(response.status_code, 403)
        self.assertFalse(ImportJob.objects.exists())

    @override_settings(NQ_CREDENTIAL_KEY=TEST_KEY)
    @patch("work_queue.tasks.repository_metadata")
    @patch("work_queue.tasks.repository_issues")
    def test_button_sync_removes_closed_issue_from_queued_work(self, fetch, metadata):
        repository = "https://github.com/example/auto"
        metadata.return_value = {
            "repository_id": 99,
            "owner_id": 42,
            "repository_url": repository,
            "default_branch": "main",
        }
        Project.objects.create(name="Auto", repository_url=repository, autonomous=True)
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
        fetch.side_effect = [[issue], [dict(issue, state="closed")]]
        self.client.post(reverse("sync_issues"))
        run_import(claim_import())
        self.assertEqual(QueueItem.objects.get().status, QueueItem.Status.QUEUED)
        self.client.post(reverse("sync_issues"))
        run_import(claim_import())
        response = self.client.get(reverse("index"), {"status": "queued"})
        self.assertEqual(response.context["items"], [])
        self.assertEqual(response.context["queue_total"], 0)
        self.assertEqual(QueueItem.objects.get().status, QueueItem.Status.CLOSED)
