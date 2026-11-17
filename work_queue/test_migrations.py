from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from django.utils import timezone

from .models import QueueItem


class AutoQueueMigrationTests(TransactionTestCase):
    def restore_schema(self):
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())

    def test_closed_status_migration_only_reconciles_queued_github_issues(self):
        executor = MigrationExecutor(connection)
        self.addCleanup(self.restore_schema)
        target = [
            (
                "work_queue",
                "0005_alter_queueitem_options_alter_queueitem_managers_and_more",
            ),
            ("projects", "0006_project_github_authority_version"),
        ]
        executor.migrate(target)
        apps = executor.loader.project_state(target).apps
        project = apps.get_model("projects", "Project").objects.create(
            name="Auto",
            repository_url="https://github.com/example/repo",
            autonomous=True,
        )
        items = apps.get_model("work_queue", "QueueItem").objects
        closed = items.create(
            project=project, source="github", issue_state="closed", title="Closed issue"
        )
        finished = timezone.now()
        unchanged = []
        for source, issue_state, status in [
            ("manual", "", "queued"),
            ("github", "open", "queued"),
            ("github", "closed", "running"),
            ("github", "closed", "succeeded"),
            ("github", "closed", "failed"),
        ]:
            item = items.create(
                project=project,
                source=source,
                issue_state=issue_state,
                status=status,
                title=status,
                branch_name=f"work/{len(unchanged)}",
                finished_at=finished,
            )
            unchanged.append(item)
        self.restore_schema()
        migrated = QueueItem.objects.get(pk=closed.pk)
        self.assertEqual(migrated.status, "closed")
        self.assertIsNotNone(migrated.finished_at)
        for original in unchanged:
            item = QueueItem.objects.get(pk=original.pk)
            self.assertEqual(item.status, original.status)
            self.assertEqual(item.finished_at, finished)
            self.assertEqual(item.branch_name, original.branch_name)
        executor = MigrationExecutor(connection)
        executor.migrate(target)
        rolled_back = items.get(pk=closed.pk)
        self.assertEqual(rolled_back.status, "queued")
        self.assertIsNone(rolled_back.finished_at)

    def test_existing_work_remains_manual_with_claim_history_preserved(self):
        executor = MigrationExecutor(connection)
        self.addCleanup(self.restore_schema)
        target = [
            ("work_queue", "0001_initial"),
            ("projects", "0003_project_credential"),
        ]
        executor.migrate(target)
        apps = executor.loader.project_state(target).apps
        project = apps.get_model("projects", "Project").objects.create(
            name="Legacy", repository_url="https://github.com/example/repo"
        )
        item = apps.get_model("work_queue", "QueueItem").objects.create(
            project=project,
            title="Existing work",
            status="succeeded",
            branch_name="nq/1",
        )
        self.restore_schema()
        migrated = QueueItem.objects.get(pk=item.pk)
        self.assertEqual(migrated.source, "manual")
        self.assertEqual(migrated.title, "Existing work")
        self.assertEqual(migrated.status, "succeeded")
        self.assertEqual(migrated.branch_name, "nq/1")
        self.assertIsNone(migrated.issue_number)

    def test_merge_preserves_imported_issue_and_execution_history(self):
        executor = MigrationExecutor(connection)
        self.addCleanup(self.restore_schema)
        target = [
            (
                "work_queue",
                "0002_queueitem_issue_number_queueitem_issue_repository_and_more",
            ),
            ("projects", "0003_project_credential"),
        ]
        executor.migrate(target)
        apps = executor.loader.project_state(target).apps
        project = apps.get_model("projects", "Project").objects.create(
            name="Imported project",
            repository_url="https://github.com/example/repo",
            autonomous=True,
        )
        item = apps.get_model("work_queue", "QueueItem").objects.create(
            project=project,
            queue="auto",
            issue_number=7,
            issue_repository=project.repository_url,
            issue_state="open",
            title="Imported issue",
            status="succeeded",
            branch_name="nq/7",
            result_url="https://github.com/example/repo/pull/8",
        )
        self.restore_schema()
        migrated = QueueItem.objects.get(pk=item.pk)
        self.assertEqual(migrated.source, "github")
        self.assertEqual(migrated.issue_number, 7)
        self.assertEqual(migrated.issue_repository, project.repository_url)
        self.assertEqual(migrated.status, "succeeded")
        self.assertEqual(migrated.branch_name, "nq/7")
        self.assertEqual(migrated.result_url, item.result_url)
