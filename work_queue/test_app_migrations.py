import uuid

from django.contrib.admin.models import ADDITION, LogEntry
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.contrib.contenttypes.models import ContentType
from django.core.management import call_command
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from django.utils import timezone

from projects.models import Project

from .models import QueueItem


class AppMigrationTests(TransactionTestCase):
    def migrate(self, targets):
        executor = MigrationExecutor(connection)
        executor.migrate(targets)
        ContentType.objects.clear_cache()
        return executor.loader.project_state(targets).apps

    def migrate_latest(self):
        executor = MigrationExecutor(connection)
        return self.migrate(executor.loader.graph.leaf_nodes())

    def setUp(self):
        apps = self.migrate([("hello", "0004_queue_execution")])
        project_model = apps.get_model("hello", "Project")
        queue_model = apps.get_model("hello", "QueueItem")
        self.project = project_model.objects.create(
            name="Existing project",
            repository_url="https://github.com/example/repo.git",
            base_branch="develop",
        )
        self.item = queue_model.objects.create(
            project=self.project,
            title="Existing work",
            instructions="Preserve context",
            priority="high",
            status="running",
            claim_token=uuid.uuid4(),
            started_at=timezone.now(),
            branch_name="nq/existing",
            execution_ref="n8n:123",
            repository_url="https://github.com/example/original.git",
            base_branch="main",
            result_url="https://github.com/example/original/pull/1",
            error_summary="Prior attempt",
        )
        self.original_project = project_model.objects.values().get(pk=self.project.pk)
        self.original_item = queue_model.objects.values().get(pk=self.item.pk)

    def tearDown(self):
        self.migrate_latest()
        super().tearDown()

    def test_upgrade_preserves_project_fields_and_id(self):
        self.migrate_latest()
        project = Project.objects.values().get(pk=self.project.pk)
        self.assertEqual(project, self.original_project)

    def test_upgrade_preserves_queue_fields_and_id(self):
        self.migrate_latest()
        item = QueueItem.objects.values().get(pk=self.item.pk)
        self.assertEqual(item, self.original_item)

    def test_upgrade_preserves_project_relationship(self):
        self.migrate_latest()
        project = Project.objects.get(pk=self.project.pk)
        self.assertEqual(project.items.get().pk, self.item.pk)

    def test_upgrade_preserves_user_queue_permissions(self):
        content_type, _ = ContentType.objects.get_or_create(
            app_label="hello", model="queueitem"
        )
        permission, _ = Permission.objects.get_or_create(
            content_type=content_type, codename="change_queueitem"
        )
        user = get_user_model().objects.create_user(username="editor", is_staff=True)
        user.user_permissions.add(permission)
        self.migrate_latest()
        user = get_user_model().objects.get(pk=user.pk)
        self.assertTrue(user.has_perm("work_queue.change_queueitem"))
        permission.refresh_from_db()
        self.assertEqual(permission.content_type_id, content_type.pk)

    def test_upgrade_preserves_group_project_permissions(self):
        content_type, _ = ContentType.objects.get_or_create(
            app_label="hello", model="project"
        )
        permission, _ = Permission.objects.get_or_create(
            content_type=content_type, codename="change_project"
        )
        group = Group.objects.create(name="Project editors")
        group.permissions.add(permission)
        user = get_user_model().objects.create_user(username="editor", is_staff=True)
        user.groups.add(group)
        self.migrate_latest()
        user = get_user_model().objects.get(pk=user.pk)
        self.assertTrue(user.has_perm("projects.change_project"))

    def test_upgrade_preserves_admin_history(self):
        content_type, _ = ContentType.objects.get_or_create(
            app_label="hello", model="queueitem"
        )
        user = get_user_model().objects.create_user(username="editor")
        entry = LogEntry.objects.create(
            user=user,
            content_type=content_type,
            object_id=str(self.item.pk),
            object_repr=self.item.title,
            action_flag=ADDITION,
        )
        self.migrate_latest()
        entry.refresh_from_db()
        self.assertEqual(
            entry.get_edited_object(), QueueItem.objects.get(pk=self.item.pk)
        )
        self.assertEqual(entry.content_type_id, content_type.pk)

    def test_latest_state_has_no_hello_models(self):
        apps = self.migrate_latest()
        self.assertNotIn("hello", apps.app_configs)

    def test_reverse_migration_restores_legacy_model_state_and_data(self):
        self.migrate_latest()
        apps = self.migrate([("hello", "0004_queue_execution")])
        queue_model = apps.get_model("hello", "QueueItem")
        item = queue_model.objects.values().get(pk=self.item.pk)
        self.assertEqual(item, self.original_item)

    def test_reverse_migration_restores_content_type_labels(self):
        self.migrate_latest()
        content_type = ContentType.objects.get_for_model(QueueItem)
        self.migrate([("hello", "0004_queue_execution")])
        content_type.refresh_from_db()
        self.assertEqual(content_type.app_label, "hello")

    def test_upgrade_keeps_claim_order_index(self):
        self.migrate_latest()
        with connection.cursor() as cursor:
            table = QueueItem._meta.db_table
            constraints = connection.introspection.get_constraints(cursor, table)
        self.assertEqual(
            constraints["queue_claim_order"]["columns"], ["status", "created_at", "id"]
        )

    def test_migrating_apps_separately_preserves_content_type_ids(self):
        content_type, _ = ContentType.objects.get_or_create(
            app_label="hello", model="project"
        )
        call_command("migrate", "hello", "0005_move_models", verbosity=0)
        call_command("migrate", "projects", verbosity=0)
        call_command("migrate", verbosity=0)
        moved = ContentType.objects.get(app_label="projects", model="project")
        self.assertEqual(moved.pk, content_type.pk)
        self.assertFalse(ContentType.objects.filter(app_label="hello").exists())
