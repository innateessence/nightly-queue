from io import StringIO

from django.core.management import call_command
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase

from projects.models import Project


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
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
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
