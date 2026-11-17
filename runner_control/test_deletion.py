from concurrent.futures import ThreadPoolExecutor
from queue import Queue
from time import monotonic, sleep
from unittest import skipUnless

from django.core.exceptions import ValidationError
from django.db import close_old_connections, connection, transaction
from django.test import Client, TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from credentials.models import (
    APISettings,
    Credential,
    CredentialSecretVersion,
    RunnerPreflightGrant,
)
from credentials.test_models import TEST_KEY
from projects.models import Project
from work_queue.models import QueueItem
from work_queue.services import (
    claim_item,
    claimable_items,
    delete_queue_item,
    enqueue_item,
    import_issue,
    source_wait_reason,
)

from .configuration import configuration_document, runner_settings
from .deletion import delete_credential, delete_project, delete_schedule
from .models import Run, RunnerIdentity
from .plan_presentation import present_payload
from .settings_models import ProjectRunnerSettings, RunnerSchedule, RunnerSettings


@override_settings(NQ_CREDENTIAL_KEY=TEST_KEY)
class DeletionTests(TestCase):
    def setUp(self):
        runner_settings()
        self.project = Project.objects.create(
            name="Example project",
            repository_url="https://github.com/example/repository",
        )
        self.item = enqueue_item(project=self.project, title="Example work")
        self.identity = RunnerIdentity.objects.create(
            name="Example machine", token_digest="a" * 64
        )

    def credential(self, name="Example credential"):
        saved = Credential(name=name, kind="token", provider="github")
        saved.set_secret("private-deletion-test-token")
        saved.save()
        return saved

    def make_run(self, *, active=False, credential=None):
        snapshot = (
            {"effective": {"implementation_credential_id": credential.pk}}
            if credential
            else {}
        )
        return Run.objects.create(
            runner=self.identity,
            item=self.item,
            state="active" if active else "completed",
            active_slot=1 if active else None,
            finished_at=None if active else timezone.now(),
            configuration_snapshot=snapshot,
        )

    def test_all_confirmation_pages_require_explicit_post(self):
        runner = RunnerSettings.objects.create(name="Example runner")
        schedule = RunnerSchedule.objects.create(name="Example schedule")
        credential = self.credential()
        rows = [
            ("coding_runner_delete", runner),
            ("runner_schedule_delete", schedule),
            ("credential_delete", credential),
            ("delete_project", self.project),
            ("delete_item", self.item),
        ]
        for route, saved in rows:
            with self.subTest(route=route):
                response = self.client.get(reverse(route, args=[saved.pk]))
                self.assertContains(response, "Cancel")
                self.assertContains(response, 'name="csrfmiddlewaretoken"')
                self.assertEqual(response["Cache-Control"].split(", ")[-1], "private")
                self.assertTrue(type(saved).objects.filter(pk=saved.pk).exists())

    def test_all_deletion_posts_enforce_csrf(self):
        client = Client(enforce_csrf_checks=True)
        runner = RunnerSettings.objects.create(name="Example runner")
        schedule = RunnerSchedule.objects.create(name="Example schedule")
        credential = self.credential()
        for route, saved in [
            ("coding_runner_delete", runner),
            ("runner_schedule_delete", schedule),
            ("credential_delete", credential),
            ("delete_project", self.project),
            ("delete_item", self.item),
        ]:
            with self.subTest(route=route):
                self.assertEqual(
                    client.post(reverse(route, args=[saved.pk])).status_code, 403
                )
                self.assertTrue(type(saved).objects.filter(pk=saved.pk).exists())

    def test_all_missing_objects_return_not_found(self):
        for route in (
            "coding_runner_delete",
            "runner_schedule_delete",
            "credential_delete",
            "delete_project",
            "delete_item",
        ):
            with self.subTest(route=route):
                self.assertEqual(
                    self.client.get(reverse(route, args=[999999])).status_code, 404
                )
                self.assertEqual(
                    self.client.post(reverse(route, args=[999999])).status_code, 404
                )

    def test_assigned_runner_deletion_disables_identity_and_preserves_history(self):
        saved = RunnerSettings.objects.create(name="Assigned", identity=self.identity)
        run = self.make_run()
        response = self.client.post(reverse("coding_runner_delete", args=[saved.pk]))
        self.assertRedirects(response, reverse("coding_runner_list"))
        self.assertFalse(RunnerSettings.objects.filter(pk=saved.pk).exists())
        self.identity.refresh_from_db()
        self.assertFalse(self.identity.enabled)
        self.assertEqual(Run.objects.get(pk=run.pk).runner_id, self.identity.pk)

    def test_active_runner_deletion_is_blocked(self):
        saved = RunnerSettings.objects.create(name="Assigned", identity=self.identity)
        self.make_run(active=True)
        response = self.client.post(reverse("coding_runner_delete", args=[saved.pk]))
        self.assertEqual(response.status_code, 409)
        self.identity.refresh_from_db()
        self.assertTrue(self.identity.enabled)
        self.assertTrue(RunnerSettings.objects.filter(pk=saved.pk).exists())

    def test_default_runner_and_schedule_explain_why_they_are_required(self):
        for route in ("coding_runner_delete", "runner_schedule_delete"):
            response = self.client.get(reverse(route, args=[1]))
            self.assertContains(response, "cannot be deleted")
            self.assertNotContains(response, 'type="submit"')
            self.assertEqual(
                self.client.post(reverse(route, args=[1])).status_code, 409
            )

    def test_schedule_deletion_reassigns_all_runners_without_changing_controls(self):
        saved = RunnerSchedule.objects.create(name="Night")
        replacement = RunnerSchedule.objects.create(name="Day")
        first = RunnerSettings.objects.create(
            name="First", schedule=saved, polling_interval_seconds=37
        )
        second = RunnerSettings.objects.create(
            name="Second", schedule=saved, paused=True
        )
        response = self.client.post(
            reverse("runner_schedule_delete", args=[saved.pk]),
            {"replacement": replacement.pk},
        )
        self.assertRedirects(response, reverse("runner_schedule_list"))
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(first.schedule_id, replacement.pk)
        self.assertEqual(second.schedule_id, replacement.pk)
        self.assertEqual(first.polling_interval_seconds, 37)
        self.assertTrue(second.paused)
        self.assertFalse(RunnerSchedule.objects.filter(pk=saved.pk).exists())

    def test_invalid_schedule_replacement_does_not_delete_or_reassign(self):
        saved = RunnerSchedule.objects.create(name="Night")
        runner = RunnerSettings.objects.create(name="Assigned", schedule=saved)
        for replacement in (saved.pk, 999999, "", "private-deletion-test-token"):
            response = self.client.post(
                reverse("runner_schedule_delete", args=[saved.pk]),
                {"replacement": replacement},
            )
            self.assertEqual(response.status_code, 409)
            runner.refresh_from_db()
            self.assertEqual(runner.schedule_id, saved.pk)
            self.assertTrue(RunnerSchedule.objects.filter(pk=saved.pk).exists())
            self.assertNotContains(
                response, "private-deletion-test-token", status_code=409
            )

    def test_unused_schedule_can_be_deleted(self):
        saved = RunnerSchedule.objects.create(name="Unused")
        delete_schedule(saved.pk)
        self.assertFalse(RunnerSchedule.objects.filter(pk=saved.pk).exists())

    def test_deleted_schedule_cannot_be_saved_from_stale_instance(self):
        saved = RunnerSchedule.objects.create(name="Unused")
        delete_schedule(saved.pk)
        with self.assertRaises(ValidationError):
            saved.save()

    def test_deleted_runner_cannot_be_saved_from_stale_instance(self):
        saved = RunnerSettings.objects.create(name="Unused")
        self.client.post(reverse("coding_runner_delete", args=[saved.pk]))
        with self.assertRaises(ValidationError):
            saved.save()

    def test_project_deletion_removes_local_work_and_retains_run_history(self):
        run = self.make_run()
        ProjectRunnerSettings.objects.create(project=self.project, overrides={})
        response = self.client.post(reverse("delete_project", args=[self.project.pk]))
        self.assertRedirects(response, reverse("project_list"))
        self.assertFalse(Project.objects.filter(pk=self.project.pk).exists())
        self.assertFalse(QueueItem.objects.filter(project_id=self.project.pk).exists())
        self.assertFalse(
            ProjectRunnerSettings.objects.filter(project_id=self.project.pk).exists()
        )
        retained = Run.objects.get(pk=run.pk)
        self.assertIsNotNone(retained.item.deleted_at)
        self.assertIsNotNone(retained.item.project.deleted_at)
        self.assertContains(
            self.client.get(reverse("runner_plan_detail", args=[run.pk])),
            "Example work",
        )

    def test_project_deletion_blocks_running_queue_items(self):
        QueueItem.objects.filter(pk=self.item.pk).update(status="running")
        response = self.client.post(reverse("delete_project", args=[self.project.pk]))
        self.assertEqual(response.status_code, 409)
        self.assertTrue(Project.objects.filter(pk=self.project.pk).exists())
        self.assertTrue(QueueItem.objects.filter(pk=self.item.pk).exists())

    def test_project_deletion_blocks_active_run_even_if_item_status_is_stale(self):
        self.make_run(active=True)
        with self.assertRaises(ValidationError):
            delete_project(self.project.pk)
        self.assertTrue(Project.objects.filter(pk=self.project.pk).exists())

    def test_deleted_project_cannot_be_saved_from_a_stale_instance(self):
        delete_project(self.project.pk)
        self.project.name = "Resurrected"
        with self.assertRaises(ValidationError):
            self.project.save()
        self.assertFalse(Project.objects.filter(pk=self.project.pk).exists())

    def test_deleted_project_cannot_receive_new_work(self):
        delete_project(self.project.pk)
        with self.assertRaises(ValidationError):
            enqueue_item(project=self.project, title="Late work")
        self.assertIsNone(claim_item(self.item))
        self.assertFalse(
            import_issue(
                project=self.project,
                number=2,
                title="Late import",
                body="",
                state="open",
            )
        )
        self.assertFalse(QueueItem.objects.exists())

    def test_deleted_project_is_absent_from_api_and_submission_choices(self):
        delete_project(self.project.pk)
        response = self.client.get(reverse("index"))
        self.assertFalse(response.context["form"].fields["project"].queryset.exists())
        self.assertNotIn(
            self.project.pk,
            [row["project_id"] for row in configuration_document()["projects"]],
        )
        self.assertEqual(
            self.client.get(
                reverse("edit_project", args=[self.project.pk])
            ).status_code,
            404,
        )

    def test_queue_deletion_retains_history_and_blocks_stale_claims(self):
        run = self.make_run()
        response = self.client.post(reverse("delete_item", args=[self.item.pk]))
        self.assertRedirects(response, reverse("index"))
        self.assertFalse(QueueItem.objects.filter(pk=self.item.pk).exists())
        self.assertIsNotNone(Run.objects.get(pk=run.pk).item.deleted_at)
        detail = self.client.get(reverse("runner_plan_detail", args=[run.pk]))
        self.assertContains(detail, "This work item was deleted.")
        self.assertNotContains(detail, "Retry task")
        self.assertIsNone(claim_item(self.item))
        self.assertFalse(claimable_items().exists())
        self.assertEqual(
            self.client.post(
                reverse("runner_operator_retry", args=[run.pk])
            ).status_code,
            409,
        )
        self.assertFalse(QueueItem.objects.exists())

    def test_running_queue_item_cannot_be_deleted(self):
        QueueItem.objects.filter(pk=self.item.pk).update(status="running")
        self.assertEqual(
            self.client.post(reverse("delete_item", args=[self.item.pk])).status_code,
            409,
        )
        self.assertTrue(QueueItem.objects.filter(pk=self.item.pk).exists())

    def test_active_run_prevents_queue_deletion_with_stale_item_status(self):
        self.make_run(active=True)
        with self.assertRaises(ValidationError):
            delete_queue_item(self.item)

    def test_deleted_work_cannot_be_saved_from_a_stale_instance(self):
        delete_queue_item(self.item)
        with self.assertRaises(ValidationError):
            self.item.save()

    def test_imported_issue_stays_removed_across_syncs_and_identity_backfill(self):
        import_issue(
            project=self.project,
            number=12,
            title="Imported",
            body="Original",
            state="open",
        )
        imported = QueueItem.objects.get(issue_number=12)
        delete_queue_item(imported)
        for repository_id in (None, 100):
            self.assertFalse(
                import_issue(
                    project=self.project,
                    number=12,
                    title="Reopened",
                    body="Updated",
                    state="open",
                    repository_id=repository_id,
                )
            )
        self.assertFalse(QueueItem.objects.filter(issue_number=12).exists())
        self.assertEqual(QueueItem.all_objects.filter(issue_number=12).count(), 1)
        self.assertEqual(
            QueueItem.all_objects.get(pk=imported.pk).instructions, "Original"
        )

    def test_deleted_branch_reservations_still_block_colliding_work(self):
        QueueItem.objects.filter(pk=self.item.pk).update(branch_name="existing-branch")
        delete_queue_item(self.item)
        other = enqueue_item(project=self.project, title="Other")
        other.requested_branch = "existing-branch"
        self.assertIn("collides", source_wait_reason(other))

    def test_unused_credential_deletion_erases_current_and_rotated_secrets(self):
        saved = self.credential()
        saved.set_secret("rotated-deletion-test-token")
        saved.save()
        self.assertTrue(
            CredentialSecretVersion.objects.filter(credential=saved).exists()
        )
        response = self.client.post(reverse("credential_delete", args=[saved.pk]))
        self.assertRedirects(response, reverse("credential_list"))
        self.assertFalse(Credential.all_objects.filter(pk=saved.pk).exists())
        self.assertFalse(
            CredentialSecretVersion.objects.filter(credential=saved).exists()
        )

    def test_credential_deletion_clears_selections_grants_and_disables_affected_processing(
        self,
    ):
        saved = self.credential()
        APISettings.objects.create(github_credential=saved)
        self.project.credential, self.project.autonomous = saved, True
        self.project.save()
        runner = RunnerSettings.objects.create(
            name="Uses credential", issue_credential=saved, enabled=True
        )
        override = ProjectRunnerSettings.objects.create(
            project=self.project,
            overrides={"issue_credential_id": {"mode": "replace", "value": saved.pk}},
        )
        RunnerPreflightGrant.objects.create(runner=self.identity, credential=saved)
        delete_credential(saved.pk)
        self.assertIsNone(APISettings.objects.get().github_credential_id)
        self.project.refresh_from_db()
        self.assertIsNone(self.project.credential_id)
        self.assertFalse(self.project.autonomous)
        runner.refresh_from_db()
        self.assertIsNone(runner.issue_credential_id)
        self.assertFalse(runner.enabled)
        self.assertTrue(runner.paused)
        override.refresh_from_db()
        self.assertEqual(override.overrides["issue_credential_id"], {"mode": "clear"})
        self.assertFalse(
            RunnerPreflightGrant.objects.filter(credential_id=saved.pk).exists()
        )

    def test_retained_credential_deletion_keeps_history_redactable_and_allows_name_reuse(
        self,
    ):
        saved = self.credential()
        run = self.make_run(credential=saved)
        delete_credential(saved.pk)
        self.assertFalse(Credential.objects.filter(pk=saved.pk).exists())
        retained = Credential.all_objects.get(pk=saved.pk)
        self.assertIsNotNone(retained.revoked_at)
        self.assertIsNotNone(retained.deleted_at)
        text, error = present_payload(
            run, "Report contains private-deletion-test-token"
        )
        self.assertEqual(error, "")
        self.assertNotIn("private-deletion-test-token", text)
        replacement = self.credential()
        self.assertNotEqual(saved.pk, replacement.pk)

    def test_active_run_prevents_credential_deletion_without_clearing_selections(self):
        saved = self.credential()
        APISettings.objects.create(github_credential=saved)
        self.make_run(active=True, credential=saved)
        response = self.client.post(reverse("credential_delete", args=[saved.pk]))
        self.assertEqual(response.status_code, 409)
        self.assertEqual(APISettings.objects.get().github_credential_id, saved.pk)
        self.assertTrue(Credential.objects.filter(pk=saved.pk).exists())

    def test_deleting_global_import_credential_disables_inheriting_projects(self):
        saved = self.credential()
        APISettings.objects.create(github_credential=saved)
        self.project.autonomous = True
        self.project.save()
        delete_credential(saved.pk)
        self.project.refresh_from_db()
        self.assertFalse(self.project.autonomous)
        self.assertIsNone(self.project.sync_lock_token)

    def test_deleting_import_credential_retires_in_flight_project_lease(self):
        from uuid import uuid4

        saved = self.credential()
        self.project.credential = saved
        self.project.save()
        QueueItem.objects.filter(pk=self.item.pk).delete()
        lease = uuid4()
        Project.objects.filter(pk=self.project.pk).update(
            sync_lock_token=lease,
            sync_lock_until=timezone.now() + timezone.timedelta(minutes=10),
        )
        delete_credential(saved.pk)
        with self.assertRaises(ValidationError):
            import_issue(
                project=self.project,
                number=1,
                title="Late import",
                body="",
                state="open",
                sync_lock_token=lease,
            )
        self.assertFalse(QueueItem.objects.exists())

    def test_deleted_credential_cannot_be_resurrected_from_stale_instance(self):
        saved = self.credential()
        delete_credential(saved.pk)
        with self.assertRaises(ValidationError):
            saved.save()

    def test_retained_deleted_credential_is_not_a_form_choice(self):
        saved = self.credential()
        self.make_run(credential=saved)
        delete_credential(saved.pk)
        response = self.client.get(reverse("api_settings"))
        self.assertFalse(
            response.context["form"]
            .fields["github_credential"]
            .queryset.filter(pk=saved.pk)
            .exists()
        )
        self.assertEqual(
            self.client.get(reverse("credential_edit", args=[saved.pk])).status_code,
            404,
        )

    def test_stale_import_settings_cannot_restore_deleted_credential_selection(self):
        saved = self.credential()
        settings = APISettings.objects.create(github_credential=saved)
        self.make_run(credential=saved)
        delete_credential(saved.pk)
        with self.assertRaises(ValidationError):
            settings.save()

    def test_delete_confirmation_redacts_secret_from_target_name(self):
        saved = self.credential()
        self.project.name = "private-deletion-test-token"
        self.project.save()
        response = self.client.get(reverse("delete_project", args=[self.project.pk]))
        self.assertNotContains(response, "private-deletion-test-token")
        self.assertNotContains(response, saved.encrypted_payload)

    def test_broken_credential_can_still_be_deleted_without_rendering_material(self):
        saved = self.credential()
        Credential.objects.filter(pk=saved.pk).update(
            encrypted_payload="broken-ciphertext"
        )
        response = self.client.get(reverse("credential_delete", args=[saved.pk]))
        self.assertContains(response, f"Credential #{saved.pk}")
        self.assertNotContains(response, "broken-ciphertext")
        self.assertEqual(
            self.client.post(reverse("credential_delete", args=[saved.pk])).status_code,
            302,
        )


@skipUnless(connection.vendor == "postgresql", "Requires PostgreSQL row locks")
class ConcurrentDeletionTests(TransactionTestCase):
    def setUp(self):
        self.project = Project.objects.create(
            name="Concurrent", repository_url="https://github.com/example/repository"
        )
        self.item = enqueue_item(project=self.project, title="Concurrent work")

    def claim_in_thread(self, pids):
        close_old_connections()
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_backend_pid()")
                pids.put(cursor.fetchone()[0])
            return claim_item(self.item)
        finally:
            connection.close()

    def wait_for_row_lock(self, pid):
        deadline = monotonic() + 10
        while monotonic() < deadline:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT EXISTS(SELECT 1 FROM pg_locks WHERE pid = %s AND NOT granted)",
                    [pid],
                )
                if cursor.fetchone()[0]:
                    return
            sleep(0.01)
        self.fail("Concurrent claim did not reach the row lock")

    def test_queue_deletion_wins_against_a_claim_waiting_on_the_project(self):
        pids = Queue()
        with ThreadPoolExecutor(max_workers=1) as pool:
            with transaction.atomic():
                Project.objects.select_for_update().get(pk=self.project.pk)
                future = pool.submit(self.claim_in_thread, pids)
                self.wait_for_row_lock(pids.get(timeout=10))
                delete_queue_item(self.item)
            self.assertIsNone(future.result(timeout=10))
        self.assertFalse(QueueItem.objects.filter(pk=self.item.pk).exists())

    def test_project_deletion_wins_against_a_claim_waiting_on_the_project(self):
        pids = Queue()
        with ThreadPoolExecutor(max_workers=1) as pool:
            with transaction.atomic():
                Project.objects.select_for_update().get(pk=self.project.pk)
                future = pool.submit(self.claim_in_thread, pids)
                self.wait_for_row_lock(pids.get(timeout=10))
                delete_project(self.project.pk)
            self.assertIsNone(future.result(timeout=10))
        self.assertFalse(Project.objects.filter(pk=self.project.pk).exists())
