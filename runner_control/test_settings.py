from django.core.exceptions import ValidationError
from django.db import transaction
from django.test import TestCase

from credentials.models import APISettings, Credential
from projects.models import Project

from .configuration import (
    configuration_document,
    effective_project_configuration,
    locked_configuration_snapshot,
)
from .settings_forms import ProjectRunnerSettingsForm, RunnerSettingsForm
from .settings_models import ProjectRunnerSettings, RunnerSettings


class RunnerSettingsTests(TestCase):
    def setUp(self):
        self.project = Project.objects.create(
            name="Example", repository_url="https://github.com/example/repo"
        )

    def test_default_execution_is_disabled_and_release_gated(self):
        document = configuration_document()
        self.assertFalse(document["global"]["enabled"])
        self.assertFalse(document["execution_ready"])
        self.assertFalse(document["projects"][0]["structurally_valid"])

    def test_sync_interval_does_not_change_configuration_revision(self):
        original = configuration_document()
        APISettings.objects.create(auto_sync_interval_hours=12)
        updated = configuration_document()
        self.assertEqual(original, updated)

    def test_model_rejects_invalid_timezone(self):
        with self.assertRaises(ValidationError):
            RunnerSettings(timezone="Not/A_Timezone").full_clean()

    def test_model_rejects_reserve_over_100(self):
        with self.assertRaises(ValidationError):
            RunnerSettings(minimum_remaining_usage_percent=101).full_clean()

    def test_model_rejects_stage_timeout_exceeding_task_timeout(self):
        with self.assertRaises(ValidationError):
            RunnerSettings(task_timeout_seconds=10).full_clean()

    def test_model_rejects_invalid_window_days(self):
        with self.assertRaises(ValidationError):
            RunnerSettings(
                windows=[{"days": [7], "start": "22:00", "end": "06:00"}]
            ).full_clean()

    def test_form_accepts_overnight_windows(self):
        saved = RunnerSettings()
        data = {
            field.name: field.value_from_object(saved)
            for field in saved._meta.fields
            if field.name != "id"
        }
        data.update(
            schedule_mode="windows",
            windows='[{"days":[0,1],"start":"22:00","end":"06:00"}]',
            setup_commands="[]",
            validation_commands="[]",
        )
        form = RunnerSettingsForm(data)
        self.assertTrue(form.is_valid(), form.errors)

    def test_override_inheritance_replace_and_clear(self):
        saved = RunnerSettings.objects.get(pk=1)
        saved.research_model = "global-model"
        saved.implementation_instructions = "global instructions"
        saved.save()
        overrides = {
            "research_model": {"mode": "replace", "value": "project-model"},
            "implementation_instructions": {"mode": "clear"},
        }
        ProjectRunnerSettings.objects.create(project=self.project, overrides=overrides)
        effective = effective_project_configuration(self.project)
        self.assertEqual(effective["effective"]["research_model"], "project-model")
        self.assertEqual(effective["effective"]["implementation_instructions"], "")
        self.assertEqual(effective["sources"]["research_backend"], "global")

    def test_blank_project_inputs_inherit(self):
        form = ProjectRunnerSettingsForm({})
        self.assertTrue(form.is_valid(), form.errors)
        form.save(self.project)
        self.assertTrue(
            all(
                entry == {"mode": "inherit"}
                for entry in self.project.runner_settings.overrides.values()
            )
        )

    def test_replace_requires_value(self):
        form = ProjectRunnerSettingsForm(
            {"research_model_mode": "replace", "research_model": ""}
        )
        self.assertFalse(form.is_valid())
        self.assertIn("research_model", form.errors)

    def test_override_rejects_unknown_fields(self):
        with self.assertRaises(ValidationError):
            ProjectRunnerSettings(
                project=self.project,
                overrides={"enabled": {"mode": "replace", "value": True}},
            ).full_clean()

    def test_snapshot_rejects_stale_configuration_revision(self):
        original = configuration_document()["revision"]
        saved = RunnerSettings.objects.get(pk=1)
        saved.paused = True
        saved.save()
        with transaction.atomic(), self.assertRaises(ValidationError):
            locked_configuration_snapshot(self.project, original)

    def test_snapshot_rejects_missing_configuration(self):
        with transaction.atomic(), self.assertRaises(ValidationError):
            locked_configuration_snapshot(
                self.project, configuration_document()["revision"]
            )

    def test_selected_credentials_serialize_metadata_only(self):
        credential = Credential.objects.create(
            name="Selected",
            provider="github",
            kind="token",
            encrypted_payload="ciphertext-hidden",
        )
        saved = RunnerSettings.objects.get(pk=1)
        saved.issue_credential = credential
        saved.save()
        document = configuration_document()
        self.assertEqual(document["credentials"][0]["id"], credential.pk)
        self.assertNotIn("encrypted_payload", str(document))
        self.assertNotIn("ciphertext-hidden", str(document))

    def test_project_create_accepts_runner_overrides(self):
        response = self.client.post(
            "/projects/",
            {
                "name": "New",
                "repository_url": "https://github.com/example/new",
                "runner-research_model_mode": "replace",
                "runner-research_model": "project-model",
            },
        )
        self.assertEqual(response.status_code, 302)
        project = Project.objects.get(name="New")
        self.assertEqual(
            project.runner_settings.overrides["research_model"]["value"],
            "project-model",
        )

    def test_settings_page_shows_runner_controls(self):
        response = self.client.get("/settings/")
        self.assertContains(response, "Coding runner")
        self.assertContains(response, "Minimum remaining usage (%)")
        self.assertContains(response, "release-gated")

    def test_settings_post_does_not_modify_import_settings(self):
        APISettings.objects.create(auto_sync_interval_hours=12)
        saved = RunnerSettings()
        data = {
            f"runner-{field.name}": field.value_from_object(saved)
            for field in saved._meta.fields
            if field.name != "id"
        }
        data.update(
            section="runner",
            **{
                "runner-paused": "on",
                "runner-windows": "[]",
                "runner-setup_commands": "[]",
                "runner-validation_commands": "[]",
            },
        )
        data = {key: "" if value is None else value for key, value in data.items()}
        response = self.client.post("/settings/", data)
        self.assertEqual(response.status_code, 302)
        self.assertTrue(RunnerSettings.objects.get(pk=1).paused)
        self.assertEqual(APISettings.objects.get(pk=1).auto_sync_interval_hours, 12)
