from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from datetime import timedelta

from django.utils import timezone

from django.db import connection, connections
from django.test import TransactionTestCase

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
        self.project.github_repository_id = 99
        self.project.github_owner_id = 42
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


class SourceClaimConcurrencyTests(TransactionTestCase):
    def setUp(self):
        from credentials.models import Credential
        from runner_control.auth import token_digest
        from runner_control.models import RunnerIdentity
        from runner_control.settings_models import RunnerSettings
        from .test_selection import selected_project

        self.project = selected_project()
        backend = Credential.objects.create(
            name="Backend",
            provider="codex",
            kind="token",
            encrypted_payload="controlled-fixture",
        )
        github = Credential.objects.create(
            name="GitHub",
            provider="github",
            kind="token",
            encrypted_payload="controlled-fixture",
        )
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
        self.assertEqual(self.item.status, "queued")

    def test_refresh_racing_requeue_cannot_restore_stale_claim(self):
        from threading import Event
        from unittest.mock import patch
        from django.core.exceptions import ValidationError
        from .services import refresh_issue_for_launch, requeue_items

        claimed = claim_next()
        fetched = Event()
        requeued = Event()

        def provider(project, number, token):
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
                item_id=first.item_id, claim_token=first.item.claim_token, token="test"
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
                item_id=first.item_id, claim_token=first.item.claim_token, token="test"
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

        def provider(project, number, token):
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
