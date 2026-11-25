from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from datetime import timedelta

from django.utils import timezone

from django.db import connection, connections
from django.test import TransactionTestCase, override_settings

from credentials.models import APISettings
from projects.models import Project

from .models import QueueItem
from .test_selection import source_issue
from .services import (
    claim_auto_sync,
    claim_item,
    claim_next,
    enqueue_item,
    import_issue,
)


class ClaimConcurrencyTests(TransactionTestCase):
    def setUp(self):
        self.project = Project.objects.create(
            name="Example", repository_url="https://github.com/example/repo"
        )
        self.item = enqueue_item(project=self.project, title="First")

    def claim(self, barrier, item_id=None):
        try:
            with connection.cursor() as cursor:
                cursor.execute("SET statement_timeout = '5s'")
            candidate = None
            if item_id is not None:
                candidate = QueueItem.objects.select_related("project").get(pk=item_id)
            barrier.wait(timeout=5)
            item = claim_next() if candidate is None else claim_item(candidate)
            return item.pk if item else None
        finally:
            connections.close_all()

    def test_only_one_worker_can_claim_the_same_candidate(self):
        barrier = Barrier(2)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(self.claim, barrier, self.item.pk) for _ in range(2)]
            claimed_ids = [future.result(timeout=10) for future in futures]
        self.assertCountEqual(claimed_ids, [self.item.pk, None])

    def test_competing_workers_claim_different_items(self):
        second = enqueue_item(project=self.project, title="Second")
        barrier = Barrier(2)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(self.claim, barrier) for _ in range(2)]
            claimed_ids = [future.result(timeout=10) for future in futures]
        self.assertCountEqual(claimed_ids, [self.item.pk, second.pk])

    def test_competing_workers_claim_manual_and_imported_items(self):
        Project.objects.filter(pk=self.project.pk).update(
            github_repository_id=99, github_owner_id=42
        )
        self.project.base_branch = "main"
        self.project.autonomous = True
        self.project.save()
        import_issue(project=self.project, **source_issue(title="Imported", body=""))
        imported = QueueItem.objects.get(source="github")
        barrier = Barrier(2)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(self.claim, barrier) for _ in range(2)]
            claimed_ids = [future.result(timeout=10) for future in futures]
        self.assertCountEqual(claimed_ids, [self.item.pk, imported.pk])


class ImportConcurrencyTests(TransactionTestCase):
    def claim_sync(self, barrier):
        try:
            saved = APISettings.objects.get()
            with connection.cursor() as cursor:
                cursor.execute("SET statement_timeout = '5s'")
            barrier.wait(timeout=5)
            return claim_auto_sync(saved)
        finally:
            connections.close_all()

    def test_only_one_worker_starts_a_due_scheduled_sync(self):
        APISettings.objects.create()
        barrier = Barrier(2)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(self.claim_sync, barrier) for _ in range(2)]
            claimed = [future.result(timeout=10) for future in futures]
        self.assertCountEqual(claimed, [True, False])

    def import_candidate(self, project_id, barrier):
        try:
            project = Project.objects.get(pk=project_id)
            with connection.cursor() as cursor:
                cursor.execute("SET statement_timeout = '5s'")
            barrier.wait(timeout=5)
            return import_issue(
                project=project, number=1, title="Issue", body="Context", state="open"
            )
        finally:
            connections.close_all()

    def test_overlapping_imports_create_one_item(self):
        project = Project.objects.create(
            name="Auto",
            repository_url="https://github.com/example/repo",
            autonomous=True,
        )
        barrier = Barrier(2)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(self.import_candidate, project.pk, barrier)
                for _ in range(2)
            ]
            created = [future.result(timeout=10) for future in futures]
        self.assertCountEqual(created, [True, False])
        self.assertEqual(QueueItem.objects.count(), 1)


@override_settings(
    VIBEARR_CREDENTIAL_KEY="MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA="
)
class SourceClaimConcurrencyTests(TransactionTestCase):
    def setUp(self):
        from credentials.models import Credential
        from runner_control.auth import token_digest
        from runner_control.models import RunnerIdentity
        from runner_control.settings_models import RunnerSettings
        from .test_selection import selected_project, source_repository
        from unittest.mock import patch

        self.project = selected_project()
        metadata = patch(
            "work_queue.github.repository_metadata", return_value=source_repository()
        )
        metadata.start()
        self.addCleanup(metadata.stop)
        backend = Credential.objects.create(
            name="Backend",
            provider="codex",
            kind="token",
            encrypted_payload="controlled-fixture",
        )
        github = Credential(name="GitHub", provider="github", kind="token")
        github.set_secret("controlled-github-token")
        github.save()
        settings = RunnerSettings.objects.get(pk=1)
        settings.enabled = True
        settings.implementation_backend = "codex"
        settings.implementation_model = "test-model"
        settings.implementation_credential = backend
        settings.issue_credential = github
        settings.repository_credential = github
        settings.validation_commands = ["python -m unittest"]
        settings.save()
        self.runner = RunnerIdentity.objects.create(
            name="Concurrent", token_digest=token_digest("test")
        )
        import_issue(project=self.project, **source_issue())
        self.item = QueueItem.objects.get(project=self.project)

    def bounded_connection(self):
        with connection.cursor() as cursor:
            cursor.execute("SET statement_timeout = '3s'")

    def test_import_project_lock_does_not_deadlock_runner_settings_lock(self):
        import uuid
        from threading import Event
        from django.db import transaction
        from runner_control.configuration import configuration_document
        from runner_control.services import claim_run, lock_controller
        from runner_control.settings_models import RunnerSettings

        project_locked = Event()
        settings_locked = Event()
        revision = configuration_document()["revision"]

        def importer():
            try:
                self.bounded_connection()
                with transaction.atomic():
                    Project.objects.select_for_update().get(pk=self.project.pk)
                    project_locked.set()
                    self.assertTrue(settings_locked.wait(5))
                    import_issue(project=self.project, **source_issue(state="closed"))
            finally:
                connections.close_all()

        def runner():
            try:
                self.bounded_connection()
                self.assertTrue(project_locked.wait(5))
                with transaction.atomic():
                    lock_controller(self.runner)
                    RunnerSettings.objects.select_for_update().get(pk=1)
                    settings_locked.set()
                    run = claim_run(
                        runner=self.runner,
                        run_id=uuid.uuid4(),
                        configuration_revision=revision,
                    )
                    return run.state
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as pool:
            imported = pool.submit(importer)
            claimed = pool.submit(runner)
            imported.result(timeout=10)
            self.assertEqual(claimed.result(timeout=10), "empty")
        self.item.refresh_from_db()
        self.assertEqual(self.item.issue_state, "closed")
        self.assertEqual(self.item.status, "closed")

    def test_refresh_racing_requeue_cannot_restore_stale_claim(self):
        from threading import Event
        from unittest.mock import patch
        from django.core.exceptions import ValidationError
        from .services import refresh_issue_for_launch, requeue_items

        claimed = claim_next()
        fetched = Event()
        requeued = Event()

        def provider(project, number, token, **kwargs):
            fetched.set()
            self.assertTrue(requeued.wait(5))
            return source_issue()

        def refresh():
            try:
                self.bounded_connection()
                with self.assertRaises(ValidationError):
                    refresh_issue_for_launch(
                        item_id=claimed.pk,
                        claim_token=claimed.claim_token,
                        token="test",
                    )
            finally:
                connections.close_all()

        def retry():
            try:
                self.bounded_connection()
                self.assertTrue(fetched.wait(5))
                requeue_items(QueueItem.objects.filter(pk=claimed.pk))
                requeued.set()
            finally:
                connections.close_all()

        with (
            patch("work_queue.github.issue_comments", return_value=[]),
            patch("work_queue.github.read_issue", side_effect=provider),
        ):
            with ThreadPoolExecutor(max_workers=2) as pool:
                refreshing = pool.submit(refresh)
                retrying = pool.submit(retry)
                refreshing.result(timeout=10)
                retrying.result(timeout=10)
        self.item.refresh_from_db()
        self.assertEqual(self.item.status, "queued")
        self.assertIsNone(self.item.claim_token)

    def test_hold_requests_cancellation_without_rewriting_running_snapshot(self):
        import uuid
        from runner_control.configuration import configuration_document
        from runner_control.services import claim_run

        run = claim_run(
            runner=self.runner,
            run_id=uuid.uuid4(),
            configuration_revision=configuration_document()["revision"],
        )
        snapshot = run.item.source_snapshot
        import_issue(
            project=self.project,
            **source_issue(body="Edited after launch", labels=["vibearr:hold"]),
        )
        run.refresh_from_db()
        run.item.refresh_from_db()
        self.assertIsNotNone(run.cancellation_requested_at)
        self.assertEqual(run.item.source_snapshot, snapshot)

    def test_closure_requests_cancellation_and_preserves_running_attempt(self):
        import uuid
        from runner_control.configuration import configuration_document
        from runner_control.services import claim_run

        run = claim_run(
            runner=self.runner,
            run_id=uuid.uuid4(),
            configuration_revision=configuration_document()["revision"],
        )
        original = run.item
        import_issue(
            project=self.project,
            **source_issue(state="closed", body="Closed after launch"),
        )
        run.refresh_from_db()
        run.item.refresh_from_db()
        self.assertIsNotNone(run.cancellation_requested_at)
        self.assertEqual(run.item.status, QueueItem.Status.RUNNING)
        self.assertEqual(run.item.claim_token, original.claim_token)
        self.assertEqual(run.item.instructions, original.instructions)
        self.assertEqual(run.item.source_snapshot, original.source_snapshot)

    def test_prior_delivery_cannot_be_selected_as_new_execution(self):
        import uuid
        from runner_control.configuration import configuration_document
        from runner_control.models import RunEvidence
        from runner_control.services import claim_run
        from .services import requeue_items

        run = claim_run(
            runner=self.runner,
            run_id=uuid.uuid4(),
            configuration_revision=configuration_document()["revision"],
        )
        RunEvidence.objects.create(
            run=run,
            operation_id=uuid.uuid4(),
            kind="verified_delivery",
            payload={"controlled": "prior delivery fixture"},
        )
        requeue_items(QueueItem.objects.filter(pk=self.item.pk))
        self.assertIsNone(claim_next())

    def test_each_run_retains_its_source_and_first_comments_after_retry(self):
        import uuid
        from unittest.mock import patch
        from runner_control.api import run_data
        from runner_control.configuration import configuration_document
        from runner_control.services import claim_run
        from .services import refresh_issue_for_launch, requeue_items

        revision = configuration_document()["revision"]
        first = claim_run(
            runner=self.runner, run_id=uuid.uuid4(), configuration_revision=revision
        )
        original = source_issue(
            created_at=self.item.issue_created_at.isoformat(),
            updated_at=self.item.issue_updated_at.isoformat(),
        )
        comments = [{"id": 123, "body": "Original discussion"}]
        with (
            patch("work_queue.github.issue_comments", return_value=comments),
            patch("work_queue.github.read_issue", return_value=original),
        ):
            refresh_issue_for_launch(
                item_id=first.item_id,
                claim_token=first.item.claim_token,
                token="test",
                runner=self.runner,
                run_id=first.pk,
            )
        first.refresh_from_db()
        saved = first.source_snapshot
        self.assertEqual(saved["comments"], comments)
        with (
            patch(
                "work_queue.github.issue_comments",
                return_value=[{"body": "Later discussion"}],
            ) as changed_comments,
            patch("work_queue.github.read_issue", return_value=original),
        ):
            refresh_issue_for_launch(
                item_id=first.item_id,
                claim_token=first.item.claim_token,
                token="test",
                runner=self.runner,
                run_id=first.pk,
            )
        changed_comments.assert_not_called()
        first.refresh_from_db()
        self.assertEqual(first.source_snapshot, saved)
        changed = {
            **original,
            "revision": "b" * 64,
            "body": "Second attempt instructions",
            "title": "Second attempt",
            "updated_at": (timezone.now() + timedelta(seconds=1)).isoformat(),
        }
        import_issue(project=self.project, **changed)
        requeue_items(QueueItem.objects.filter(pk=self.item.pk))
        second = claim_run(
            runner=self.runner, run_id=uuid.uuid4(), configuration_revision=revision
        )
        first.refresh_from_db()
        self.assertEqual(first.source_snapshot, saved)
        self.assertEqual(run_data(first)["source_snapshot"], saved)
        self.assertEqual(second.source_snapshot["revision"], "b" * 64)
        self.assertEqual(second.source_snapshot["body"], "Second attempt instructions")
        self.assertNotIn("comments", second.source_snapshot)

    def test_claim_snapshot_survives_retry_without_successful_refresh(self):
        import uuid
        from runner_control.configuration import configuration_document
        from runner_control.services import claim_run
        from .services import requeue_items

        first = claim_run(
            runner=self.runner,
            run_id=uuid.uuid4(),
            configuration_revision=configuration_document()["revision"],
        )
        saved = first.source_snapshot
        self.assertEqual(saved["body"], "Acceptance: tests pass")
        import_issue(
            project=self.project,
            **source_issue(body="Later instructions", revision="b" * 64),
        )
        requeue_items(QueueItem.objects.filter(pk=self.item.pk))
        first.refresh_from_db()
        self.assertEqual(first.source_snapshot, saved)

    def test_delayed_old_provider_reply_cannot_override_newer_sync(self):
        from threading import Event
        from unittest.mock import patch
        from django.core.exceptions import ValidationError
        from .services import refresh_issue_for_launch

        claimed = claim_next()
        original = source_issue(
            created_at=self.item.issue_created_at.isoformat(),
            updated_at=self.item.issue_updated_at.isoformat(),
        )
        fetched = Event()
        newer_saved = Event()

        def provider(project, number, token, **kwargs):
            fetched.set()
            self.assertTrue(newer_saved.wait(5))
            return original

        def refresh():
            try:
                self.bounded_connection()
                with self.assertRaisesMessage(ValidationError, "changed after claim"):
                    refresh_issue_for_launch(
                        item_id=claimed.pk,
                        claim_token=claimed.claim_token,
                        token="test",
                    )
            finally:
                connections.close_all()

        def synchronize():
            try:
                self.bounded_connection()
                self.assertTrue(fetched.wait(5))
                changed = {
                    **original,
                    "revision": "b" * 64,
                    "body": "Newer instructions",
                    "updated_at": (timezone.now() + timedelta(seconds=1)).isoformat(),
                }
                import_issue(project=self.project, **changed)
                newer_saved.set()
            finally:
                connections.close_all()

        with (
            patch("work_queue.github.issue_comments", return_value=[]),
            patch("work_queue.github.read_issue", side_effect=provider),
        ):
            with ThreadPoolExecutor(max_workers=2) as pool:
                refreshing = pool.submit(refresh)
                syncing = pool.submit(synchronize)
                refreshing.result(timeout=10)
                syncing.result(timeout=10)
        self.item.refresh_from_db()
        self.assertEqual(self.item.issue_revision, "b" * 64)
        self.assertEqual(self.item.source_snapshot["revision"], original["revision"])

    def test_revoked_refresh_cannot_change_second_attempt_metadata_or_history(self):
        import uuid
        from threading import Event
        from unittest.mock import patch
        from runner_control.configuration import configuration_document
        from runner_control.models import Run
        from runner_control.services import RunConflict, claim_run
        from .services import refresh_issue_for_launch, requeue_items

        revision = configuration_document()["revision"]
        first = claim_run(
            runner=self.runner, run_id=uuid.uuid4(), configuration_revision=revision
        )
        fetched = Event()
        reclaimed = Event()
        expected = {}
        closed = source_issue(
            state="closed",
            revision="c" * 64,
            body="Revoked response",
            labels=["vibearr:hold"],
        )

        def provider(project, number, token, **kwargs):
            fetched.set()
            self.assertTrue(reclaimed.wait(5))
            return closed

        def refresh():
            try:
                self.bounded_connection()
                with self.assertRaises(RunConflict):
                    refresh_issue_for_launch(
                        item_id=first.item_id,
                        claim_token=first.item.claim_token,
                        token="test",
                        runner=self.runner,
                        run_id=first.pk,
                    )
            finally:
                connections.close_all()

        def retry():
            try:
                self.bounded_connection()
                self.assertTrue(fetched.wait(5))
                requeue_items(QueueItem.objects.filter(pk=self.item.pk))
                second = claim_run(
                    runner=self.runner,
                    run_id=uuid.uuid4(),
                    configuration_revision=revision,
                )
                expected["item"] = QueueItem.objects.values().get(pk=second.item_id)
                expected["first"] = Run.objects.values().get(pk=first.pk)
                expected["second"] = Run.objects.values().get(pk=second.pk)
                expected["project"] = Project.objects.values().get(pk=self.project.pk)
                expected["second_id"] = second.pk
                reclaimed.set()
            finally:
                connections.close_all()

        with (
            patch(
                "work_queue.github.issue_comments",
                return_value=[{"body": "Revoked discussion"}],
            ),
            patch("work_queue.github.read_issue", side_effect=provider),
        ):
            with ThreadPoolExecutor(max_workers=2) as pool:
                refreshing = pool.submit(refresh)
                retrying = pool.submit(retry)
                refreshing.result(timeout=10)
                retrying.result(timeout=10)
        self.assertEqual(
            QueueItem.objects.values().get(pk=self.item.pk), expected["item"]
        )
        self.assertEqual(Run.objects.values().get(pk=first.pk), expected["first"])
        self.assertEqual(
            Run.objects.values().get(pk=expected["second_id"]), expected["second"]
        )
        self.assertEqual(
            Project.objects.values().get(pk=self.project.pk), expected["project"]
        )

    def test_old_refresh_cannot_overwrite_real_newer_importer_observation(self):
        import uuid
        from threading import Event
        from unittest.mock import patch
        from django.core.exceptions import ValidationError
        from credentials.models import APISettings, Credential
        from runner_control.configuration import configuration_document
        from runner_control.models import Run
        from runner_control.services import claim_run
        from .services import refresh_issue_for_launch
        from .tasks import import_auto_issues
        from .test_selection import source_repository

        APISettings.objects.create(
            github_credential=Credential.objects.get(name="GitHub")
        )
        run = claim_run(
            runner=self.runner,
            run_id=uuid.uuid4(),
            configuration_revision=configuration_document()["revision"],
        )
        original = source_issue(
            created_at=self.item.issue_created_at.isoformat(),
            updated_at=self.item.issue_updated_at.isoformat(),
        )
        old_repository = source_repository()
        newer_repository = source_repository(
            owner_id=777,
            default_branch="trunk",
            repository_url="https://github.com/new-owner/repo.git",
        )
        final_read = Event()
        synchronized = Event()
        observations = []
        expected_item = QueueItem.objects.values().get(pk=self.item.pk)
        expected_run = Run.objects.values().get(pk=run.pk)
        accepted = {}

        def repository(*args, **kwargs):
            observations.append(True)
            if len(observations) == 2:
                final_read.set()
                self.assertTrue(synchronized.wait(5))
            return old_repository

        def refresh():
            try:
                self.bounded_connection()
                with self.assertRaisesMessage(
                    ValidationError, "Repository authority changed"
                ):
                    refresh_issue_for_launch(
                        item_id=run.item_id,
                        claim_token=run.item.claim_token,
                        token="test",
                        runner=self.runner,
                        run_id=run.pk,
                    )
            finally:
                connections.close_all()

        def synchronize():
            try:
                self.bounded_connection()
                self.assertTrue(final_read.wait(5))
                self.assertEqual(import_auto_issues()["projects"], 1)
                accepted.update(Project.objects.values().get(pk=self.project.pk))
                self.assertEqual(accepted["github_owner_id"], 777)
                synchronized.set()
            finally:
                connections.close_all()

        with (
            patch("work_queue.github.repository_metadata", side_effect=repository),
            patch("work_queue.github.issue_comments", return_value=[]),
            patch("work_queue.github.read_issue", return_value=original),
            patch(
                "work_queue.tasks.repository_metadata", return_value=newer_repository
            ),
            patch("work_queue.tasks.repository_issues", return_value=[]),
        ):
            with ThreadPoolExecutor(max_workers=2) as pool:
                refreshing = pool.submit(refresh)
                syncing = pool.submit(synchronize)
                refreshing.result(timeout=10)
                syncing.result(timeout=10)
        self.assertEqual(Project.objects.values().get(pk=self.project.pk), accepted)
        self.assertEqual(accepted["github_authority_version"], 1)
        self.assertEqual(QueueItem.objects.values().get(pk=self.item.pk), expected_item)
        self.assertEqual(Run.objects.values().get(pk=run.pk), expected_run)

    def test_old_importer_cannot_overwrite_newer_refresh_authority(self):
        import uuid
        from threading import Event
        from unittest.mock import patch
        from django.core.exceptions import ValidationError
        from credentials.models import APISettings, Credential
        from runner_control.configuration import configuration_document
        from runner_control.models import Run
        from runner_control.services import claim_run
        from .services import refresh_issue_for_launch, repository_authority_state
        from .tasks import import_auto_issues
        from .test_selection import source_repository

        APISettings.objects.create(
            github_credential=Credential.objects.get(name="GitHub")
        )
        run = claim_run(
            runner=self.runner,
            run_id=uuid.uuid4(),
            configuration_revision=configuration_document()["revision"],
        )
        original = source_issue(
            created_at=self.item.issue_created_at.isoformat(),
            updated_at=self.item.issue_updated_at.isoformat(),
        )
        imported_read = Event()
        refreshed = Event()
        newer_repository = source_repository(
            owner_id=777,
            default_branch="trunk",
            repository_url="https://github.com/new-owner/repo.git",
        )
        expected = {}

        def importer_repository(*args, **kwargs):
            imported_read.set()
            self.assertTrue(refreshed.wait(5))
            return source_repository()

        def synchronize():
            try:
                self.bounded_connection()
                with self.assertLogs("work_queue.tasks", level="WARNING"):
                    result = import_auto_issues()
                self.assertEqual(result["failed"], 1)
                self.assertEqual(result["projects"], 0)
            finally:
                connections.close_all()

        def refresh():
            try:
                self.bounded_connection()
                self.assertTrue(imported_read.wait(5))
                with self.assertRaisesMessage(
                    ValidationError, "author is not permitted"
                ):
                    refresh_issue_for_launch(
                        item_id=run.item_id,
                        claim_token=run.item.claim_token,
                        token="test",
                        runner=self.runner,
                        run_id=run.pk,
                    )
                expected["authority"] = repository_authority_state(
                    Project.objects.get(pk=self.project.pk)
                )
                expected["item"] = QueueItem.objects.values().get(pk=self.item.pk)
                expected["run"] = Run.objects.values().get(pk=run.pk)
                refreshed.set()
            finally:
                connections.close_all()

        with (
            patch(
                "work_queue.tasks.repository_metadata", side_effect=importer_repository
            ),
            patch("work_queue.tasks.repository_issues", return_value=[]) as issues,
            patch(
                "work_queue.github.repository_metadata", return_value=newer_repository
            ),
            patch("work_queue.github.issue_comments", return_value=[]),
            patch("work_queue.github.read_issue", return_value=original),
        ):
            with ThreadPoolExecutor(max_workers=2) as pool:
                syncing = pool.submit(synchronize)
                refreshing = pool.submit(refresh)
                syncing.result(timeout=10)
                refreshing.result(timeout=10)
        self.assertEqual(
            repository_authority_state(Project.objects.get(pk=self.project.pk)),
            expected["authority"],
        )
        self.assertEqual(expected["authority"]["github_owner_id"], 777)
        self.assertEqual(expected["authority"]["github_authority_version"], 1)
        self.assertEqual(
            QueueItem.objects.values().get(pk=self.item.pk), expected["item"]
        )
        self.assertEqual(Run.objects.values().get(pk=run.pk), expected["run"])
        issues.assert_not_called()


class ProjectSyncEditConcurrencyTests(TransactionTestCase):
    def test_sync_metadata_survives_concurrent_stale_form_save(self):
        import uuid
        from threading import Event
        from django.db import transaction
        from django.utils import timezone
        from projects.forms import ProjectForm
        from runner_control.settings_models import RunnerSettings

        project = Project.objects.create(
            name="Original", repository_url="https://github.com/example/repo"
        )
        synced = Event()
        settings_locked = Event()
        metadata = dict(
            github_repository_id=99,
            github_owner_id=42,
            github_default_branch="trunk",
            sync_lock_token=uuid.uuid4(),
            sync_lock_until=timezone.now() + timedelta(minutes=10),
            sync_etags={"1": {"etag": "fresh"}},
        )

        def synchronize():
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SET statement_timeout = '3s'")
                with transaction.atomic():
                    Project.objects.select_for_update().get(pk=project.pk)
                    Project.objects.filter(pk=project.pk).update(**metadata)
                    synced.set()
                    self.assertTrue(settings_locked.wait(5))
            finally:
                connections.close_all()

        def edit():
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SET statement_timeout = '3s'")
                self.assertTrue(synced.wait(5))
                form = ProjectForm(
                    dict(
                        name="Edited",
                        repository_url=project.repository_url,
                        base_branch="",
                        permitted_author_ids="[]",
                    ),
                    instance=project,
                )
                self.assertTrue(form.is_valid(), form.errors)
                with transaction.atomic():
                    RunnerSettings.objects.select_for_update().get(pk=1)
                    settings_locked.set()
                    form.save()
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as pool:
            syncing = pool.submit(synchronize)
            editing = pool.submit(edit)
            syncing.result(timeout=10)
            editing.result(timeout=10)
        project.refresh_from_db()
        self.assertEqual(project.name, "Edited")
        for key, value in metadata.items():
            self.assertEqual(getattr(project, key), value)
