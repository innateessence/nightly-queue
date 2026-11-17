import json
from datetime import datetime, timezone
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase
from django.urls import reverse

from vibearr_runner.gates import schedule
from work_queue.services import enqueue_item

from .configuration import configuration_document
from .auth import token_digest
from .models import RunnerIdentity
from .services import claim_run
from .settings_models import RunnerSchedule, RunnerSettings
from .test_multiple_runners import form_data
from .test_settings import ConfiguredProjectTestCase


class SharedScheduleViewsTests(TestCase):
    def test_schedules_navigation_follows_coding_runners(self):
        response = self.client.get(reverse("runner_schedule_list"))
        self.assertContains(response, "Schedules")
        content = response.content.decode()
        self.assertLess(
            content.index('href="/coding-runners/"'),
            content.index('href="/schedules/"'),
        )
        self.assertContains(response, 'href="/schedules/" aria-current="page"')

    def test_create_multiple_named_schedules(self):
        for name in ("Morning", "Evening", "Weekend"):
            response = self.client.post(
                reverse("runner_schedule_add"),
                {
                    "name": name,
                    "schedule_mode": "continuous",
                    "timezone": "UTC",
                    "windows": "[]",
                },
            )
            saved = RunnerSchedule.objects.get(name=name)
            self.assertRedirects(
                response, reverse("runner_schedule_edit", args=[saved.pk])
            )
        self.assertEqual(RunnerSchedule.objects.count(), 4)

    def test_runner_selects_one_saved_schedule(self):
        selected = RunnerSchedule.objects.create(
            name="Evening", timezone="Europe/London"
        )
        data = form_data(RunnerSettings(name="Second"), schedule=selected.pk)
        response = self.client.post(reverse("coding_runner_add"), data)
        self.assertEqual(response.status_code, 302)
        saved = RunnerSettings.objects.get(name="Second")
        self.assertEqual(saved.schedule, selected)
        response = self.client.get(reverse("coding_runner_edit", args=[saved.pk]))
        fields = response.context["runner_form"].fields
        self.assertIn("schedule", fields)
        for name in ("schedule_mode", "timezone", "windows"):
            self.assertNotIn(name, fields)

    def test_runner_requires_a_schedule(self):
        data = form_data(RunnerSettings.objects.get(pk=1), schedule="")
        response = self.client.post(reverse("coding_runner_edit", args=[1]), data)
        self.assertFormError(
            response, "runner_form", "schedule", "This field is required."
        )
        self.assertEqual(RunnerSettings.objects.get(pk=1).schedule_id, 1)

    def test_unknown_schedule_assignment_is_rejected(self):
        data = form_data(RunnerSettings.objects.get(pk=1), schedule=987654)
        response = self.client.post(reverse("coding_runner_edit", args=[1]), data)
        self.assertIn("schedule", response.context["runner_form"].errors)
        self.assertEqual(RunnerSettings.objects.get(pk=1).schedule_id, 1)

    def test_invalid_timezone_does_not_create_schedule(self):
        response = self.client.post(
            reverse("runner_schedule_add"),
            {
                "name": "Invalid",
                "schedule_mode": "continuous",
                "timezone": "Not/A_Zone",
                "windows": "[]",
            },
        )
        self.assertIn("timezone", response.context["form"].errors)
        self.assertFalse(RunnerSchedule.objects.filter(name="Invalid").exists())

    def test_invalid_windows_do_not_replace_saved_schedule(self):
        saved = RunnerSchedule.objects.create(name="Saved")
        response = self.client.post(
            reverse("runner_schedule_edit", args=[saved.pk]),
            {
                "name": saved.name,
                "schedule_mode": "windows",
                "timezone": "UTC",
                "windows": '[{"days":[7],"start":"22:00","end":"06:00"}]',
            },
        )
        self.assertIn("windows", response.context["form"].errors)
        saved.refresh_from_db()
        self.assertEqual(saved.schedule_mode, "continuous")

    def test_duplicate_schedule_name_is_rejected(self):
        RunnerSchedule.objects.create(name="Night")
        response = self.client.post(
            reverse("runner_schedule_add"),
            {
                "name": "Night",
                "schedule_mode": "continuous",
                "timezone": "UTC",
                "windows": "[]",
            },
        )
        self.assertIn("name", response.context["form"].errors)
        self.assertEqual(RunnerSchedule.objects.filter(name="Night").count(), 1)

    def test_assigned_schedule_cannot_be_deleted(self):
        selected = RunnerSchedule.objects.create(name="Assigned")
        RunnerSettings.objects.create(name="Second", schedule=selected)
        response = self.client.post(
            reverse("runner_schedule_delete", args=[selected.pk])
        )
        self.assertEqual(response.status_code, 409)
        self.assertTrue(RunnerSchedule.objects.filter(pk=selected.pk).exists())

    def test_unassigned_schedule_can_be_deleted(self):
        saved = RunnerSchedule.objects.create(name="Unused")
        response = self.client.post(reverse("runner_schedule_delete", args=[saved.pk]))
        self.assertRedirects(response, reverse("runner_schedule_list"))
        self.assertFalse(RunnerSchedule.objects.filter(pk=saved.pk).exists())

    def test_default_schedule_cannot_be_deleted(self):
        response = self.client.post(reverse("runner_schedule_delete", args=[1]))
        self.assertEqual(response.status_code, 409)

    def test_shared_schedule_edit_updates_all_assigned_identities(self):
        windows = [{"days": [0], "start": "22:00", "end": "06:00"}]
        shared = RunnerSchedule.objects.create(
            name="Shared", schedule_mode="windows", windows=windows
        )
        identities = []
        for name in ("first", "second"):
            identity = RunnerIdentity.objects.create(
                name=name, token_digest=token_digest(name)
            )
            RunnerSettings.objects.create(name=name, identity=identity, schedule=shared)
            identities.append(identity)
        unrelated = RunnerIdentity.objects.create(name="other", token_digest="c" * 64)
        RunnerSettings.objects.create(name="Other", identity=unrelated)
        before = [configuration_document(identity) for identity in identities]
        other_before = configuration_document(unrelated)
        now = datetime(2026, 11, 16, 20, tzinfo=timezone.utc)
        for document in before:
            self.assertIsNone(schedule(document["global"], now)[0])
        changed = [{"days": [0], "start": "19:00", "end": "21:00"}]
        response = self.client.post(
            reverse("runner_schedule_edit", args=[shared.pk]),
            {
                "name": shared.name,
                "schedule_mode": "windows",
                "timezone": "UTC",
                "windows": json.dumps(changed),
            },
        )
        self.assertEqual(response.status_code, 302)
        for identity, original in zip(identities, before):
            current = configuration_document(identity)
            self.assertNotEqual(current["revision"], original["revision"])
            self.assertEqual(current["global"]["windows"], changed)
            self.assertIsNotNone(schedule(current["global"], now)[0])
        self.assertEqual(configuration_document(unrelated), other_before)


class SharedScheduleClaimTests(ConfiguredProjectTestCase):
    def test_schedule_edit_rejects_stale_claim_preflight(self):
        from .test_support import provision_controller

        self.runner = RunnerIdentity.objects.create(name="claim", token_digest="d" * 64)
        provision_controller(self)
        item = enqueue_item(project=self.project, title="Wait for current schedule")
        original = configuration_document(self.runner)
        selected = RunnerSchedule.objects.get(pk=1)
        selected.timezone = "Europe/London"
        selected.save()
        with self.assertRaises(ValidationError):
            claim_run(
                runner=self.runner,
                run_id=uuid4(),
                configuration_revision=original["revision"],
            )
        item.refresh_from_db()
        self.assertEqual(item.status, "queued")


class SharedScheduleMigrationTests(TransactionTestCase):
    def test_default_schedule_can_be_recreated_after_a_database_reset(self):
        call_command("flush", interactive=False, verbosity=0)
        additional = RunnerSchedule.objects.create(name="After reset")
        self.assertGreater(additional.pk, 1)
        self.assertEqual(RunnerSchedule.objects.get(pk=1).name, "Always on")

    def migrate(self, targets):
        executor = MigrationExecutor(connection)
        executor.migrate(targets)
        return executor.loader.project_state(targets).apps

    def restore_schema(self):
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())

    def test_existing_windows_and_timezones_survive_migration_and_rollback(self):
        self.addCleanup(self.restore_schema)
        target = [
            (
                "runner_control",
                "0014_runnersettings_identity_runnersettings_name_and_more",
            )
        ]
        apps = self.migrate(target)
        LegacySettings = apps.get_model("runner_control", "RunnerSettings")
        windows = [{"days": [0, 1, 2, 3, 4], "start": "22:00", "end": "06:00"}]
        LegacySettings.objects.update_or_create(
            pk=1,
            defaults={
                "name": "Legacy",
                "schedule_mode": "windows",
                "timezone": "America/Los_Angeles",
                "windows": windows,
            },
        )
        second = LegacySettings.objects.create(
            pk=2, name="Second", timezone="Europe/London"
        )
        self.restore_schema()
        saved = RunnerSettings.objects.get(pk=1)
        self.assertEqual(saved.schedule.windows, windows)
        self.assertEqual(saved.schedule.timezone, "America/Los_Angeles")
        self.assertEqual(
            RunnerSettings.objects.get(pk=second.pk).schedule.timezone, "Europe/London"
        )
        largest = max(RunnerSchedule.objects.values_list("pk", flat=True))
        self.assertGreater(RunnerSchedule.objects.create(name="Additional").pk, largest)
        apps = self.migrate(target)
        LegacySettings = apps.get_model("runner_control", "RunnerSettings")
        restored = LegacySettings.objects.get(pk=1)
        self.assertEqual(restored.windows, windows)
        self.assertEqual(restored.timezone, "America/Los_Angeles")
