import json
from datetime import datetime, timezone
from uuid import uuid4

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase
from django.urls import reverse

from credentials.models import APISettings
from vibearr_runner.gates import schedule
from work_queue.services import enqueue_item

from .auth import token_digest
from .configuration import configuration_document, settings_values
from .execution_policy import current_native_policy
from .models import Run, RunnerIdentity
from .native_services import authorize
from .services import RunConflict, claim_run
from .settings_models import RunnerSettings
from .test_settings import ConfiguredProjectTestCase
from .test_support import provision_controller


def form_data(saved, **changes):
    data = {}
    for field in saved._meta.fields:
        if field.name == "id":
            continue
        value = field.value_from_object(saved)
        if field.get_internal_type() == "JSONField":
            value = json.dumps(value)
        data[f"runner-{field.name}"] = "" if value is None else value
    data.update({f"runner-{key}": value for key, value in changes.items()})
    return data


class CodingRunnerConfigurationViewsTests(TestCase):
    def test_configuration_navigation_links_to_runner_section(self):
        response = self.client.get(reverse("api_settings"))
        self.assertContains(response, f'href="{reverse("coding_runner_list")}"')
        self.assertNotContains(response, 'name="runner-schedule_mode"')

    def test_create_multiple_runners_with_independent_schedules(self):
        APISettings.objects.create(auto_sync_interval_seconds=3600)
        for name, start, zone in [
            ("Morning", "08:00", "UTC"),
            ("Evening", "22:00", "America/Los_Angeles"),
        ]:
            windows = [{"days": [0, 1, 2, 3, 4], "start": start, "end": "23:00"}]
            data = form_data(
                RunnerSettings(name=name),
                schedule_mode="windows",
                windows=json.dumps(windows),
                timezone=zone,
            )
            response = self.client.post(reverse("coding_runner_add"), data)
            saved = RunnerSettings.objects.get(name=name)
            self.assertRedirects(
                response, reverse("coding_runner_edit", args=[saved.pk])
            )
            self.assertEqual(saved.windows, windows)
            self.assertEqual(saved.timezone, zone)
        self.assertEqual(RunnerSettings.objects.count(), 3)
        self.assertEqual(APISettings.objects.get().auto_sync_interval_seconds, 3600)
        response = self.client.get(reverse("coding_runner_list"))
        self.assertContains(response, "Morning")
        self.assertContains(response, "Evening")

    def test_editing_one_schedule_preserves_other_schedules(self):
        other = RunnerSettings.objects.create(name="Other", timezone="Europe/London")
        data = form_data(other, timezone="America/New_York", tasks_per_window=5)
        response = self.client.post(
            reverse("coding_runner_edit", args=[other.pk]), data
        )
        self.assertEqual(response.status_code, 302)
        other.refresh_from_db()
        self.assertEqual(other.timezone, "America/New_York")
        self.assertEqual(other.tasks_per_window, 5)
        self.assertEqual(RunnerSettings.objects.get(pk=1).timezone, "UTC")

    def test_invalid_schedule_does_not_create_runner(self):
        data = form_data(
            RunnerSettings(name="Invalid"), schedule_mode="windows", windows="[]"
        )
        response = self.client.post(reverse("coding_runner_add"), data)
        self.assertFormError(
            response, "runner_form", "windows", "Add at least one processing window."
        )
        self.assertFalse(RunnerSettings.objects.filter(name="Invalid").exists())

    def test_identity_cannot_be_assigned_to_two_configurations(self):
        identity = RunnerIdentity.objects.create(name="night", token_digest="a" * 64)
        RunnerSettings.objects.create(name="First", identity=identity)
        data = form_data(RunnerSettings(name="Second"), identity=identity.pk)
        response = self.client.post(reverse("coding_runner_add"), data)
        self.assertIn("identity", response.context["runner_form"].errors)
        self.assertFalse(RunnerSettings.objects.filter(name="Second").exists())

    def test_unassigned_runner_can_be_deleted(self):
        saved = RunnerSettings.objects.create(name="Unused")
        response = self.client.post(reverse("coding_runner_delete", args=[saved.pk]))
        self.assertRedirects(response, reverse("coding_runner_list"))
        self.assertFalse(RunnerSettings.objects.filter(pk=saved.pk).exists())

    def test_default_runner_cannot_be_deleted(self):
        response = self.client.post(reverse("coding_runner_delete", args=[1]))
        self.assertEqual(response.status_code, 409)
        self.assertTrue(RunnerSettings.objects.filter(pk=1).exists())

    def test_assigned_runner_cannot_be_deleted(self):
        identity = RunnerIdentity.objects.create(name="night", token_digest="a" * 64)
        saved = RunnerSettings.objects.create(name="Assigned", identity=identity)
        response = self.client.post(reverse("coding_runner_delete", args=[saved.pk]))
        self.assertEqual(response.status_code, 409)
        self.assertTrue(RunnerSettings.objects.filter(pk=saved.pk).exists())


class MultipleRunnerExecutionTests(ConfiguredProjectTestCase):
    def setUp(self):
        super().setUp()
        self.token = "night-runner-token-" + "a" * 32
        self.runner = RunnerIdentity.objects.create(
            name="night", token_digest=token_digest(self.token)
        )
        provision_controller(self)
        self.client.defaults["HTTP_AUTHORIZATION"] = f"Bearer {self.token}"
        values = settings_values(RunnerSettings.objects.get(pk=1))
        values.update(
            enabled=True,
            implementation_model="night-model",
            timezone="America/Los_Angeles",
        )
        self.settings = RunnerSettings.objects.create(
            name="Night", identity=self.runner, **values
        )

    def test_machine_configuration_selects_authenticated_identity(self):
        response = self.client.get(reverse("runner_configuration"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["runner_configuration_id"], self.settings.pk)
        self.assertEqual(response.json()["global"]["timezone"], "America/Los_Angeles")
        self.assertEqual(
            response.json()["projects"][0]["effective"]["implementation_model"],
            "night-model",
        )
        self.assertFalse(configuration_document()["global"]["enabled"])

    def test_unassigned_identity_uses_existing_default(self):
        identity = RunnerIdentity.objects.create(name="legacy", token_digest="b" * 64)
        self.assertEqual(configuration_document(identity), configuration_document())

    def test_claim_snapshots_selected_configuration_while_default_disabled(self):
        item = enqueue_item(project=self.project, title="Night work")
        document = configuration_document(self.runner)
        run = claim_run(
            runner=self.runner,
            run_id=uuid4(),
            configuration_revision=document["revision"],
        )
        self.assertEqual(run.item_id, item.pk)
        self.assertEqual(
            run.configuration_snapshot["runner_configuration_id"], self.settings.pk
        )
        self.assertEqual(
            run.configuration_snapshot["effective"]["implementation_model"],
            "night-model",
        )

    def test_other_runner_edit_does_not_invalidate_preflight(self):
        enqueue_item(project=self.project, title="Night work")
        document = configuration_document(self.runner)
        default = RunnerSettings.objects.get(pk=1)
        default.timezone = "Europe/London"
        default.save()
        run = claim_run(
            runner=self.runner,
            run_id=uuid4(),
            configuration_revision=document["revision"],
        )
        self.assertEqual(run.state, Run.State.ACTIVE)

    def test_selected_runner_edit_invalidates_preflight(self):
        item = enqueue_item(project=self.project, title="Wait")
        document = configuration_document(self.runner)
        self.settings.paused = True
        self.settings.save()
        response = self.client.post(
            reverse("runner_claim"),
            json.dumps(
                {"run_id": str(uuid4()), "configuration_revision": document["revision"]}
            ),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)
        item.refresh_from_db()
        self.assertEqual(item.status, "queued")

    def test_schedules_open_independently_for_each_identity(self):
        self.settings.schedule_mode = "windows"
        self.settings.windows = [{"days": [0], "start": "22:00", "end": "06:00"}]
        self.settings.save()
        now = datetime(2026, 11, 16, 20, tzinfo=timezone.utc)
        night_interval, _ = schedule(configuration_document(self.runner)["global"], now)
        default_interval, _ = schedule(configuration_document()["global"], now)
        self.assertIsNone(night_interval)
        self.assertIsNotNone(default_interval)

    def test_assignment_cannot_change_during_active_work(self):
        enqueue_item(project=self.project, title="Active")
        claim_run(
            runner=self.runner,
            run_id=uuid4(),
            configuration_revision=configuration_document(self.runner)["revision"],
        )
        data = form_data(self.settings, identity="")
        response = self.client.post(
            reverse("coding_runner_edit", args=[self.settings.pk]), data
        )
        self.assertIn("identity", response.context["runner_form"].errors)
        self.settings.refresh_from_db()
        self.assertEqual(self.settings.identity, self.runner)

    def test_native_authorization_uses_assigned_controls(self):
        enqueue_item(project=self.project, title="Active")
        run = claim_run(
            runner=self.runner,
            run_id=uuid4(),
            configuration_revision=configuration_document(self.runner)["revision"],
        )
        self.assertEqual(
            authorize(run).digest, run.configuration_snapshot["policy_digest"]
        )
        self.assertEqual(
            current_native_policy(run, run.item.claim_token).digest,
            run.configuration_snapshot["policy_digest"],
        )
        self.settings.paused = True
        self.settings.save()
        with self.assertRaisesMessage(RunConflict, "disabled or paused"):
            authorize(run)

    def test_assignment_change_fences_native_execution(self):
        enqueue_item(project=self.project, title="Active")
        run = claim_run(
            runner=self.runner,
            run_id=uuid4(),
            configuration_revision=configuration_document(self.runner)["revision"],
        )
        self.settings.identity = None
        self.settings.save()
        with self.assertRaisesMessage(RunConflict, "assignment changed"):
            authorize(run)


class MultipleRunnerMigrationTests(TransactionTestCase):
    def restore_schema(self):
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())

    def test_migration_preserves_legacy_schedule_and_allocates_new_ids(self):
        self.addCleanup(self.restore_schema)
        target = [("runner_control", "0013_nativedeliveryrecovery")]
        executor = MigrationExecutor(connection)
        executor.migrate(target)
        apps = executor.loader.project_state(target).apps
        legacy = apps.get_model("runner_control", "RunnerSettings")
        windows = [{"days": [0], "start": "22:00", "end": "06:00"}]
        legacy.objects.update_or_create(
            pk=1,
            defaults={
                "enabled": True,
                "schedule_mode": "windows",
                "timezone": "America/Los_Angeles",
                "windows": windows,
            },
        )
        self.restore_schema()
        saved = RunnerSettings.objects.get(pk=1)
        self.assertTrue(saved.enabled)
        self.assertEqual(saved.windows, windows)
        self.assertEqual(saved.timezone, "America/Los_Angeles")
        self.assertEqual(saved.name, "Default runner")
        additional = RunnerSettings.objects.create(name="Additional")
        self.assertGreater(additional.pk, 1)
