from django.db import IntegrityError, transaction
from django.db.models.deletion import ProtectedError
from django.test import TestCase
from django.urls import reverse

from projects.models import Project

from .models import QueueItem
from .services import claim_next, enqueue_item


class ProjectRelationshipTests(TestCase):
    def setUp(self):
        url = "https://github.com/example/repo"
        self.project = Project.objects.create(name="Example", repository_url=url)

    def test_referenced_project_cannot_be_deleted(self):
        enqueue_item(project=self.project, title="Work")
        with self.assertRaises(ProtectedError):
            self.project.delete()

    def test_project_lists_its_queue_items(self):
        item = enqueue_item(project=self.project, title="Work")
        self.assertEqual(list(self.project.items.all()), [item])

    def test_enqueue_trims_title(self):
        item = enqueue_item(project=self.project, title=" Implement export ")
        self.assertEqual(item.title, "Implement export")

    def test_queue_item_requires_a_project_at_database_level(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            QueueItem.objects.create(title="Missing project")


class ProjectViewTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        url = "https://github.com/example/repo"
        cls.project = Project.objects.create(name="Example", repository_url=url)

    def assert_project_form_error(self, project):
        data = {
            "project": project,
            "title": "Keep me",
            "instructions": "Context",
            "priority": "normal",
        }
        response = self.client.post(reverse("index"), data)
        self.assertEqual(response.status_code, 200)
        self.assertIn("project", response.context["form"].errors)
        self.assertContains(response, "Context")
        self.assertFalse(QueueItem.objects.exists())

    def assert_ignored_project_filter(self, project):
        item = enqueue_item(project=self.project, title="Work")
        response = self.client.get(reverse("index"), {"project": project})
        self.assertEqual(list(response.context["items"]), [item])
        self.assertFalse(response.context["filtered"])

    def test_form_requires_project(self):
        self.assert_project_form_error("")

    def test_form_rejects_unknown_project(self):
        self.assert_project_form_error("99999")

    def test_project_is_displayed_with_repository_link(self):
        enqueue_item(project=self.project, title="Work")
        response = self.client.get(reverse("index"))
        self.assertContains(response, self.project.repository_url)
        self.assertContains(response, self.project.name)

    def test_project_filter_combines_with_search_and_priority(self):
        url = "https://github.com/example/other"
        other = Project.objects.create(name="Other", repository_url=url)
        match = enqueue_item(project=self.project, title="Export CSV", priority="high")
        enqueue_item(project=other, title="Export CSV", priority="high")
        enqueue_item(project=self.project, title="Export JSON", priority="low")
        filters = {"project": self.project.pk, "q": "export", "priority": "high"}
        response = self.client.get(reverse("index"), filters)
        self.assertEqual(list(response.context["items"]), [match])
        self.assertEqual(response.context["queue_count"], 3)

    def test_non_numeric_project_filter_is_ignored(self):
        self.assert_ignored_project_filter("missing")

    def test_unknown_project_filter_is_ignored(self):
        self.assert_ignored_project_filter("99999")

    def test_large_project_filter_is_ignored(self):
        self.assert_ignored_project_filter("9" * 100)

    def test_negative_project_filter_is_ignored(self):
        self.assert_ignored_project_filter("-1")

    def test_empty_project_list_explains_how_to_add_one(self):
        self.project.delete()
        response = self.client.get(reverse("index"))
        self.assertContains(response, "Create a project")

    def test_running_item_cannot_be_removed(self):
        item = enqueue_item(project=self.project, title="Work")
        claim_next()
        url = reverse("remove_item", args=[item.pk])
        self.client.post(url)
        self.assertTrue(QueueItem.objects.filter(pk=item.pk).exists())

    def test_queue_count_excludes_running_items(self):
        enqueue_item(project=self.project, title="Work")
        claim_next()
        response = self.client.get(reverse("index"))
        self.assertEqual(response.context["queue_count"], 0)

    def test_running_status_is_displayed(self):
        enqueue_item(project=self.project, title="Work")
        claim_next()
        response = self.client.get(reverse("index"))
        self.assertContains(response, "Running")
