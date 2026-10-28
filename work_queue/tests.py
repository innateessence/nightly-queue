from django.conf import settings
from django.test import Client, TestCase
from django.urls import reverse

from projects.models import Project

from .models import QueueItem
from .tasks import add, nightly_greeting


class QueueViewTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.project = Project.objects.create(
            name="Nightly", repository_url="https://github.com/example/nightly-queue"
        )

    def create_item(self, **kwargs):
        return QueueItem.objects.create(project=self.project, **kwargs)

    def assert_invalid_item(self, field, value):
        data = {
            "project": self.project.pk,
            "title": "Work",
            "instructions": "Keep this context",
            "priority": "normal",
        }
        data[field] = value
        response = self.client.post(reverse("index"), data)
        self.assertEqual(response.status_code, 200)
        self.assertIn(field, response.context["form"].errors)
        self.assertContains(response, "Keep this context")
        self.assertFalse(QueueItem.objects.exists())

    def test_index_renders(self):
        response = self.client.get(reverse("index"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Leave it for the night.")
        self.assertContains(response, "Add your first item")
        self.assertContains(response, "n8n can claim queued work through the API")
        self.assertEqual(response.context["queue_count"], 0)

    def test_add_item_persists_and_redirects(self):
        data = {
            "project": self.project.pk,
            "title": "Research competitors",
            "instructions": "Compare pricing and features.",
            "priority": "high",
        }
        response = self.client.post(reverse("index"), data, follow=True)

        self.assertRedirects(response, reverse("index"))
        item = QueueItem.objects.get()
        self.assertEqual(item.title, "Research competitors")
        self.assertEqual(item.instructions, "Compare pricing and features.")
        self.assertEqual(item.priority, QueueItem.Priority.HIGH)
        self.assertContains(response, item.title)
        self.assertContains(response, "High priority")
        self.assertContains(response, "Work item added to your queue.")
        self.assertEqual(response.context["queue_count"], 1)

        self.client.get(reverse("index"))
        self.assertEqual(QueueItem.objects.count(), 1)

    def test_instructions_are_optional(self):
        data = {
            "project": self.project.pk,
            "title": "Draft an outline",
            "priority": "normal",
        }
        response = self.client.post(reverse("index"), data)

        self.assertRedirects(response, reverse("index"))
        self.assertEqual(QueueItem.objects.get().instructions, "")

    def test_blank_title_is_rejected(self):
        self.assert_invalid_item("title", "   ")

    def test_long_title_is_rejected(self):
        self.assert_invalid_item("title", "x" * 201)

    def test_unknown_priority_is_rejected(self):
        self.assert_invalid_item("priority", "urgent")

    def test_empty_post_shows_validation_errors(self):
        response = self.client.post(reverse("index"), {})

        self.assertIn("title", response.context["form"].errors)
        self.assertFalse(QueueItem.objects.exists())

    def test_queue_displays_oldest_first_regardless_of_priority(self):
        first = self.create_item(title="First item", priority=QueueItem.Priority.LOW)
        second = self.create_item(title="Second item", priority=QueueItem.Priority.HIGH)

        response = self.client.get(reverse("index"))

        self.assertEqual(list(response.context["items"]), [first, second])
        self.assertEqual(response.context["queue_count"], 2)
        self.assertNotContains(response, "Add your first item")

    def test_work_item_title_is_escaped(self):
        self.create_item(title="<script>alert('title')</script>")
        response = self.client.get(reverse("index"))
        self.assertContains(response, "&lt;script&gt;")
        self.assertNotContains(response, "<script>alert('title')</script>")

    def test_work_item_instructions_are_escaped(self):
        instructions = "<img src=x onerror=alert(1)>"
        self.create_item(title="Work", instructions=instructions)
        response = self.client.get(reverse("index"))
        self.assertContains(response, "&lt;img")
        self.assertNotContains(response, "<img")

    def test_search_matches_title_and_instructions_and_preserves_total(self):
        title_match = self.create_item(title="Research pricing")
        context_match = self.create_item(
            title="Write a report", instructions="Include PRICING comparisons."
        )
        self.create_item(title="Prepare slides")

        response = self.client.get(reverse("index"), {"q": " pricing "})

        items = list(response.context["items"])
        self.assertEqual(items, [title_match, context_match])
        self.assertEqual(response.context["queue_count"], 3)
        self.assertContains(response, "2 items found")

    def test_priority_filter_combines_with_search(self):
        match = self.create_item(title="Pricing report", priority="high")
        self.create_item(title="Pricing notes", priority="low")
        self.create_item(title="Prepare slides", priority="high")

        filters = {"q": "pricing", "priority": "high"}
        response = self.client.get(reverse("index"), filters)

        self.assertEqual(list(response.context["items"]), [match])
        self.assertEqual(response.context["queue_count"], 3)

    def test_search_with_no_matches_shows_filtered_empty_state(self):
        self.create_item(title="Prepare slides")

        response = self.client.get(reverse("index"), {"q": "missing"})

        self.assertContains(response, "No matching work items.")
        self.assertContains(response, "Clear filters")
        self.assertNotContains(response, "Add your first item")

    def test_unknown_priority_does_not_hide_items(self):
        item = self.create_item(title="Prepare slides")

        response = self.client.get(reverse("index"), {"priority": "unknown"})

        self.assertEqual(list(response.context["items"]), [item])
        self.assertFalse(response.context["filtered"])

    def test_remove_item(self):
        item = self.create_item(title="Remove me")
        other = self.create_item(title="Keep me")

        url = reverse("remove_item", args=[item.pk])
        response = self.client.post(url, follow=True)

        self.assertRedirects(response, reverse("index"))
        self.assertEqual(list(QueueItem.objects.all()), [other])
        self.assertContains(response, "Work item removed from your queue.")

    def test_remove_requires_post(self):
        item = self.create_item(title="Keep me")

        response = self.client.get(reverse("remove_item", args=[item.pk]))

        self.assertEqual(response.status_code, 405)
        self.assertTrue(QueueItem.objects.filter(pk=item.pk).exists())

    def test_remove_missing_item_returns_not_found(self):
        response = self.client.post(reverse("remove_item", args=[999]))

        self.assertEqual(response.status_code, 404)

    def test_add_requires_csrf(self):
        client = Client(enforce_csrf_checks=True)
        data = {"project": self.project.pk, "title": "No token", "priority": "normal"}
        response = client.post(reverse("index"), data)
        self.assertEqual(response.status_code, 403)
        self.assertFalse(QueueItem.objects.exists())

    def test_remove_requires_csrf(self):
        client = Client(enforce_csrf_checks=True)
        item = self.create_item(title="Keep me")
        url = reverse("remove_item", args=[item.pk])
        response = client.post(url)
        self.assertEqual(response.status_code, 403)
        self.assertTrue(QueueItem.objects.filter(pk=item.pk).exists())


class CeleryTaskTests(TestCase):
    def test_add_task_returns_sum(self):
        result = add(2, 3)
        self.assertEqual(result, 5)

    def test_nightly_greeting_task(self):
        result = nightly_greeting()
        self.assertIn("greeting", result)
        self.assertIsNotNone(result.get("at"))

    def test_beat_schedule_contains_nightly_greeting(self):
        self.assertIn("nightly-greeting", settings.CELERY_BEAT_SCHEDULE)
        greeting = settings.CELERY_BEAT_SCHEDULE["nightly-greeting"]
        self.assertEqual(greeting["task"], "work_queue.tasks.nightly_greeting")
