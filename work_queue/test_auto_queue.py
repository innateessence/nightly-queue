import json

from django.test import TestCase, override_settings
from django.urls import reverse

from projects.models import Project

from .models import QueueItem
from .test_selection import source_issue
from .services import claim_item, claim_next, enqueue_item, import_issue


@override_settings(VIBEARR_API_TOKEN="test-token")
class UnifiedQueueTests(TestCase):
    def setUp(self):
        self.project = Project.objects.create(
            name="Auto",
            repository_url="https://github.com/example/repo",
            autonomous=True,
            github_repository_id=99,
            github_owner_id=42,
            base_branch="main",
        )
        import_issue(
            project=self.project,
            number=7,
            title="Imported issue",
            body="<script>context</script>",
            state="open",
        )
        self.imported = QueueItem.objects.get()
        import_issue(
            project=self.project,
            **source_issue(7, title="Imported issue", body="<script>context</script>"),
        )
        self.manual = enqueue_item(project=self.project, title="Manual work")

    def claim(self, data):
        return self.client.post(
            reverse("api_claim"),
            json.dumps(data),
            content_type="application/json",
            HTTP_AUTHORIZATION="Bearer test-token",
        )

    def test_queue_displays_both_sources_in_creation_order(self):
        response = self.client.get(reverse("index"))
        self.assertEqual(list(response.context["items"]), [self.imported, self.manual])
        self.assertEqual(response.context["queue_count"], 2)
        self.assertContains(response, "GitHub issue")
        self.assertContains(response, "Manual")
        self.assertNotContains(response, ">Auto queue</a>")

    def test_search_matches_both_sources(self):
        response = self.client.get(reverse("index"), {"q": "context"})
        self.assertEqual(list(response.context["items"]), [self.imported])
        response = self.client.get(reverse("index"), {"q": "manual"})
        self.assertEqual(list(response.context["items"]), [self.manual])

    def test_old_auto_url_redirects_to_queue_and_preserves_filters(self):
        response = self.client.get(reverse("auto_queue"), {"q": "missing"})
        self.assertRedirects(response, reverse("index") + "?q=missing")

    def test_issue_content_is_escaped(self):
        response = self.client.get(reverse("index"))
        self.assertContains(response, "https://github.com/example/repo/issues/7")
        self.assertContains(response, "&lt;script&gt;")
        self.assertNotContains(response, "<script>context</script>")

    def test_default_claim_processes_both_sources_oldest_first(self):
        first = self.claim({}).json()["item"]
        self.assertEqual(first["id"], self.imported.pk)
        self.assertEqual(first["source"], "github")
        self.assertEqual(first["issue_number"], 7)
        second = self.claim({}).json()["item"]
        self.assertEqual(second["id"], self.manual.pk)
        self.assertEqual(second["source"], "manual")
        self.assertIsNone(self.claim({}).json()["item"])

    def test_earlier_manual_work_is_claimed_before_later_issue(self):
        QueueItem.objects.filter(pk=self.imported.pk).update(status="succeeded")
        import_issue(
            project=self.project, **source_issue(8, title="Later issue", body="")
        )
        self.assertEqual(claim_next().pk, self.manual.pk)
        self.assertEqual(claim_next().issue_number, 8)

    def test_queue_selection_is_no_longer_accepted(self):
        response = self.claim({"queue": "auto"})
        self.assertEqual(response.status_code, 400)

    def test_closed_issue_remains_visible_without_blocking_manual_work(self):
        QueueItem.objects.filter(pk=self.imported.pk).update(issue_state="closed")
        response = self.client.get(reverse("index"))
        self.assertEqual(list(response.context["items"]), [self.imported, self.manual])
        self.assertEqual(response.context["queue_count"], 1)
        self.assertEqual(claim_next().pk, self.manual.pk)
        self.assertIsNone(claim_next())

    def test_issue_closed_since_candidate_read_cannot_be_claimed(self):
        QueueItem.objects.filter(pk=self.imported.pk).update(issue_state="closed")
        self.assertIsNone(claim_item(self.imported))

    def test_disabling_autonomous_skips_imported_work_and_keeps_manual_work(self):
        self.project.autonomous = False
        self.project.save()
        self.assertEqual(claim_next().pk, self.manual.pk)
        self.assertIsNone(claim_next())

    def test_manual_submission_cannot_spoof_imported_source(self):
        self.client.post(
            reverse("index"),
            {
                "project": self.project.pk,
                "title": "Submitted",
                "priority": "normal",
                "source": "github",
            },
        )
        self.assertEqual(QueueItem.objects.get(title="Submitted").source, "manual")

    def test_removing_imported_item_does_not_break_deduplication(self):
        response = self.client.post(reverse("remove_item", args=[self.imported.pk]))
        self.assertRedirects(response, reverse("index"))
        self.assertFalse(QueueItem.objects.filter(pk=self.imported.pk).exists())
        self.assertIsNotNone(QueueItem.all_objects.get(pk=self.imported.pk).deleted_at)
