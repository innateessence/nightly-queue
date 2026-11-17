from datetime import timedelta

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import SystemLog


class SystemLogViewTests(TestCase):
    def entry(
        self,
        message="Sync finished.",
        level="info",
        source="work_queue.tasks",
        **fields,
    ):
        return SystemLog.objects.create(
            level=level, source=source, message=message, **fields
        )

    def test_logs_follow_settings_in_configuration_navigation(self):
        response = self.client.get(reverse("system_logs"))
        html = response.content.decode()
        self.assertLess(html.index('href="/settings/"'), html.index('href="/logs/"'))
        self.assertContains(response, 'href="/logs/" aria-current="page"')
        self.assertContains(response, "System Logs")
        self.assertContains(response, 'aria-disabled="true">Runner Logs')

    def test_empty_page_explains_when_logs_appear(self):
        response = self.client.get(reverse("system_logs"))
        self.assertContains(response, "No system logs yet")
        self.assertContains(response, "Application events will appear here")
        self.assertEqual(SystemLog.objects.count(), 0)

    def test_newest_events_are_first(self):
        older = self.entry("First")
        newer = self.entry("Second")
        response = self.client.get(reverse("system_logs"))
        self.assertEqual(list(response.context["page"]), [newer, older])

    def test_filters_combine_message_level_and_source(self):
        match = self.entry("Sync failed", "error", "work_queue.tasks")
        self.entry("Sync complete", "info", "work_queue.tasks")
        self.entry("Sync failed", "error", "credentials.views")
        response = self.client.get(
            reverse("system_logs"),
            {"q": "failed", "level": "error", "source": "work_queue.tasks"},
        )
        self.assertEqual(list(response.context["page"]), [match])

    def test_search_includes_exception_details(self):
        match = self.entry(details="ConnectionError")
        response = self.client.get(reverse("system_logs"), {"q": "connectionerror"})
        self.assertEqual(list(response.context["page"]), [match])

    def test_filters_with_no_results_offer_clear_action(self):
        response = self.client.get(reverse("system_logs"), {"q": "missing"})
        self.assertContains(response, "No matching logs")
        self.assertContains(response, "Clear filters")

    def test_messages_and_details_are_escaped(self):
        self.entry("<script>alert(1)</script>", details="<img src=x onerror=alert(1)>")
        response = self.client.get(reverse("system_logs"))
        self.assertContains(response, "&lt;script&gt;")
        self.assertNotContains(response, "<script>alert(1)</script>")
        self.assertNotContains(response, "<img src=x")

    def test_pagination_preserves_encoded_filters(self):
        SystemLog.objects.bulk_create(
            [
                SystemLog(level="warning", source="fixture", message="a & b")
                for _ in range(51)
            ]
        )
        response = self.client.get(
            reverse("system_logs"),
            {"q": "a & b", "level": "warning", "source": "fixture", "page": "2"},
        )
        self.assertEqual(len(response.context["page"]), 1)
        self.assertContains(
            response, "q=a+%26+b&amp;level=warning&amp;source=fixture&amp;page=1"
        )
        self.assertContains(response, "Page 2 of 2")

    def test_invalid_page_and_level_are_handled(self):
        self.entry()
        response = self.client.get(
            reverse("system_logs"), {"page": "9" * 5000, "level": "<script>"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["page"].number, 1)
        self.assertEqual(response.context["level"], "")

    def test_expired_events_are_not_visible_even_without_a_new_write(self):
        self.entry("Expired", created_at=timezone.now() - timedelta(days=8))
        response = self.client.get(reverse("system_logs"))
        self.assertEqual(response.context["page"].paginator.count, 0)
        self.assertNotContains(response, "Expired")

    def test_logs_are_never_cached(self):
        response = self.client.get(reverse("system_logs"))
        self.assertIn("no-store", response["Cache-Control"])
        self.assertIn("private", response["Cache-Control"])

    def test_post_does_not_modify_logs(self):
        self.entry()
        response = self.client.post(reverse("system_logs"))
        self.assertEqual(response.status_code, 405)
        self.assertTrue(SystemLog.objects.filter(message="Sync finished.").exists())
