from urllib.parse import parse_qs

from django.test import TestCase
from django.urls import reverse

from projects.models import Project
from .models import QueueItem


class QueueStatusUITests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.project = Project.objects.create(
            name="UI project", repository_url="https://github.com/example/ui"
        )
        for status in QueueItem.Status.values:
            QueueItem.objects.create(project=cls.project, title=status, status=status)

    def test_each_status_filters_items_and_preserves_global_queue_total(self):
        for status in QueueItem.Status.values:
            with self.subTest(status=status):
                response = self.client.get(reverse("index"), {"status": status})
                self.assertEqual(
                    [item.status for item in response.context["items"]], [status]
                )
                self.assertEqual(response.context["queue_total"], 1)
                self.assertContains(response, f'value="{status}" selected')

    def test_malformed_status_falls_back_to_all_items(self):
        for status in ["unknown", "RUNNING", "queued,running", "<script>", " " * 20]:
            with self.subTest(status=status):
                response = self.client.get(reverse("index"), {"status": status})
                self.assertEqual(len(response.context["items"]), 4)
                self.assertEqual(response.context["status"], "")
                self.assertFalse(response.context["filtered"])

    def test_status_combines_with_search_project_and_priority(self):
        item = QueueItem.objects.create(
            project=self.project, title="Target task", status="failed", priority="high"
        )
        response = self.client.get(
            reverse("index"),
            {
                "status": "failed",
                "q": "Target",
                "priority": "high",
                "project": self.project.pk,
            },
        )
        self.assertEqual(list(response.context["items"]), [item])

    def test_status_links_preserve_encoded_search_and_other_filters(self):
        filters = {
            "q": "a & b + c",
            "priority": "high",
            "project": str(self.project.pk),
        }
        response = self.client.get(reverse("index"), {**filters, "status": "queued"})
        for link in response.context["status_links"]:
            with self.subTest(status=link["value"]):
                query = parse_qs(link["query"])
                for name, value in filters.items():
                    self.assertEqual(query[name], [value])
                self.assertEqual(query.get("status", [""]), [link["value"]])

    def test_clear_filters_keeps_status_and_removes_other_filters(self):
        response = self.client.get(
            reverse("index"), {"status": "failed", "q": "missing"}
        )
        self.assertEqual(response.context["clear_filters_query"], "status=failed")
        self.assertContains(response, 'href="/?status=failed"')
        cleared = self.client.get(
            reverse("index") + "?" + response.context["clear_filters_query"]
        )
        self.assertEqual([item.status for item in cleared.context["items"]], ["failed"])

    def test_all_work_link_removes_only_status(self):
        response = self.client.get(
            reverse("index"), {"status": "failed", "q": "queued"}
        )
        all_link = response.context["status_links"][0]
        self.assertEqual(parse_qs(all_link["query"]), {"q": ["queued"]})

    def test_status_counters_are_not_multiplied_by_multiple_items(self):
        QueueItem.objects.create(project=self.project, title="Another queued item")
        response = self.client.get(reverse("index"))
        counts = {
            tab["value"]: tab["count"] for tab in response.context["status_links"]
        }
        self.assertEqual(
            counts, {"": 5, "queued": 2, "running": 1, "succeeded": 1, "failed": 1}
        )
