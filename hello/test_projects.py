from io import StringIO

from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.db.models.deletion import ProtectedError
from django.test import TestCase, TransactionTestCase
from django.urls import reverse

from .models import Project, QueueItem
from .services import claim_next, enqueue_item


class ProjectTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.project = Project.objects.create(
            name="Example",
            repository_url="https://github.com/example/repo/",
            base_branch="develop",
        )

    def assert_invalid_repository(self, repository_url):
        self.project.repository_url = repository_url
        with self.assertRaises(ValidationError):
            self.project.save()

    def assert_invalid_branch(self, branch):
        self.project.base_branch = branch
        with self.assertRaises(ValidationError):
            self.project.save()

    def test_repository_is_normalized_to_clone_url(self):
        expected = "https://github.com/example/repo.git"
        self.assertEqual(self.project.repository_url, expected)

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

    def test_repository_rejects_non_github_host(self):
        self.assert_invalid_repository("https://gitlab.com/example/repo")

    def test_repository_requires_https(self):
        self.assert_invalid_repository("http://github.com/example/repo")

    def test_repository_rejects_credentials(self):
        self.assert_invalid_repository("https://token@github.com/example/repo")

    def test_repository_rejects_subpage(self):
        self.assert_invalid_repository("https://github.com/example/repo/issues")

    def test_repository_rejects_query_string(self):
        self.assert_invalid_repository("https://github.com/example/repo?token=secret")

    def test_repository_rejects_fragment(self):
        self.assert_invalid_repository("https://github.com/example/repo#main")

    def test_repository_rejects_empty_name(self):
        self.assert_invalid_repository("https://github.com/owner/.git")

    def test_repository_rejects_malformed_url(self):
        self.assert_invalid_repository("https://[invalid/owner/repo")

    def test_branch_rejects_leading_option(self):
        self.assert_invalid_branch("-option")

    def test_branch_rejects_double_dot(self):
        self.assert_invalid_branch("a..b")

    def test_branch_rejects_trailing_slash(self):
        self.assert_invalid_branch("feature/")

    def test_branch_rejects_hidden_component(self):
        self.assert_invalid_branch(".hidden")

    def test_branch_rejects_lock_suffix(self):
        self.assert_invalid_branch("a.lock")

    def test_branch_rejects_spaces(self):
        self.assert_invalid_branch("a b")

    def test_branch_rejects_reflog_expression(self):
        self.assert_invalid_branch("x@{y")

    def test_branch_rejects_backslash(self):
        self.assert_invalid_branch("x\\y")

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


class LegacyMigrationTests(TransactionTestCase):
    def migrate(self, name):
        target = [("hello", name)]
        executor = MigrationExecutor(connection)
        executor.migrate(target)
        return executor.loader.project_state(target).apps

    def setUp(self):
        apps = self.migrate("0001_initial")
        queue_item = apps.get_model("hello", "QueueItem")
        legacy = queue_item.objects.create(
            title="Legacy work", instructions="Preserve this", priority="high"
        )
        self.legacy_id = legacy.pk
        apps = self.migrate("0002_projects")
        self.queue_item = apps.get_model("hello", "QueueItem")
        url = "https://github.com/example/actual"
        self.project = Project.objects.create(name="Actual repo", repository_url=url)

    def tearDown(self):
        # Restore the schema before Django flushes the test database.
        apps = self.migrate("0002_projects")
        project_model = apps.get_model("hello", "Project")
        queue_item = apps.get_model("hello", "QueueItem")
        url = "https://github.com/example/restore.git"
        project = project_model.objects.create(name="Restore", repository_url=url)
        unassigned = queue_item.objects.filter(project__isnull=True)
        unassigned.update(project=project)
        self.migrate("0004_queue_execution")
        super().tearDown()

    def test_upgrade_requires_explicit_project_assignment(self):
        message = "Existing queue items need projects"
        with self.assertRaisesMessage(RuntimeError, message):
            self.migrate("0003_require_project")

    def test_backfill_assigns_selected_item(self):
        output = StringIO()
        item_ids = [self.legacy_id]
        project_id = self.project.pk
        call_command(
            "backfill_projects", project=project_id, items=item_ids, stdout=output
        )
        item = self.queue_item.objects.get(pk=self.legacy_id)
        self.assertEqual(item.project_id, project_id)
        self.assertIn("Assigned 1 item(s)", output.getvalue())

    def test_backfill_leaves_unselected_items_unassigned(self):
        unselected = self.queue_item.objects.create(title="Unselected")
        item_ids = [self.legacy_id]
        project_id = self.project.pk
        output = StringIO()
        call_command(
            "backfill_projects", project=project_id, items=item_ids, stdout=output
        )
        unselected.refresh_from_db()
        self.assertIsNone(unselected.project_id)

    def test_upgrade_preserves_existing_work(self):
        project_id = self.project.pk
        output = StringIO()
        call_command("backfill_projects", project=project_id, all=True, stdout=output)
        apps = self.migrate("0004_queue_execution")
        queue_item = apps.get_model("hello", "QueueItem")
        item = queue_item.objects.get(pk=self.legacy_id)
        self.assertEqual(item.project_id, project_id)
        self.assertEqual(item.title, "Legacy work")
        self.assertEqual(item.instructions, "Preserve this")
        self.assertEqual(item.priority, "high")
        self.assertEqual(item.status, "queued")

    def test_backfill_all_assigns_unassigned_items(self):
        project_id = self.project.pk
        output = StringIO()
        call_command("backfill_projects", project=project_id, all=True, stdout=output)
        item = self.queue_item.objects.get(pk=self.legacy_id)
        self.assertEqual(item.project_id, project_id)

    def test_backfill_all_leaves_assigned_items_unchanged(self):
        url = "https://github.com/example/second"
        second = Project.objects.create(name="Second", repository_url=url)
        assigned = self.queue_item.objects.create(
            project_id=self.project.pk, title="Already assigned"
        )
        output = StringIO()
        call_command("backfill_projects", project=second.pk, all=True, stdout=output)
        assigned.refresh_from_db()
        self.assertEqual(assigned.project_id, self.project.pk)
