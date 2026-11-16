import json
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from threading import Event

from django.core.exceptions import ValidationError
from django.db import connection, connections, transaction
from django.test import TestCase, TransactionTestCase

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

    def test_model_rejects_empty_object_windows(self):
        with self.assertRaisesMessage(ValidationError, "list of at most 32 windows"):
            RunnerSettings(windows={}).full_clean()

    def test_model_rejects_empty_object_commands(self):
        with self.assertRaisesMessage(ValidationError, "list of at most 100 commands"):
            RunnerSettings(setup_commands={}).full_clean()

    def test_model_rejects_empty_list_overrides(self):
        with self.assertRaisesMessage(ValidationError, "supported project override"):
            ProjectRunnerSettings(project=self.project, overrides=[]).save()
        self.assertFalse(ProjectRunnerSettings.objects.exists())
        self.assertEqual(
            configuration_document()["projects"][0]["project_id"], self.project.pk
        )

    def post_setup_commands(self, value):
        saved = RunnerSettings.objects.get(pk=1)
        data = {
            f"runner-{field.name}": field.value_from_object(saved)
            for field in saved._meta.fields
            if field.name != "id"
        }
        data.update(
            section="runner",
            **{
                "runner-windows": "[]",
                "runner-setup_commands": value,
                "runner-validation_commands": "[]",
            },
        )
        data = {key: "" if value is None else value for key, value in data.items()}
        return self.client.post("/settings/", data)

    def test_settings_post_rejects_null_commands_without_database_error(self):
        response = self.post_setup_commands("null")
        self.assertEqual(response.status_code, 200)
        self.assertFormError(
            response,
            "runner_form",
            "setup_commands",
            "Enter a list of at most 100 commands.",
        )
        self.assertEqual(RunnerSettings.objects.get(pk=1).setup_commands, [])

    def test_settings_post_rejects_null_command_characters(self):
        response = self.post_setup_commands(json.dumps(["echo \x00"]))
        self.assertEqual(response.status_code, 200)
        self.assertFormError(
            response,
            "runner_form",
            "setup_commands",
            "Text cannot contain null characters.",
        )
        self.assertEqual(RunnerSettings.objects.get(pk=1).setup_commands, [])

    def test_settings_post_rejects_unpaired_command_surrogates(self):
        response = self.post_setup_commands(json.dumps(["echo \ud800"]))
        self.assertEqual(response.status_code, 200)
        self.assertFormError(
            response, "runner_form", "setup_commands", "Enter valid Unicode text."
        )
        self.assertEqual(RunnerSettings.objects.get(pk=1).setup_commands, [])

    def test_settings_post_preserves_unicode_and_literal_escapes(self):
        commands = ["echo \U0001f600 \u00e9", r"echo \u0000"]
        response = self.post_setup_commands(json.dumps(commands))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(RunnerSettings.objects.get(pk=1).setup_commands, commands)

    def test_model_rejects_null_override_instructions(self):
        overrides = {
            "research_instructions": {
                "mode": "replace",
                "value": "research\x00instructions",
            }
        }
        with self.assertRaisesMessage(ValidationError, "null characters"):
            ProjectRunnerSettings(project=self.project, overrides=overrides).save()
        self.assertFalse(ProjectRunnerSettings.objects.exists())

    def test_model_rejects_unpaired_override_command_surrogates(self):
        overrides = {"setup_commands": {"mode": "replace", "value": ["echo \ud800"]}}
        with self.assertRaisesMessage(ValidationError, "valid Unicode text"):
            ProjectRunnerSettings(project=self.project, overrides=overrides).save()
        self.assertFalse(ProjectRunnerSettings.objects.exists())

    def test_project_post_rejects_unpaired_command_surrogates(self):
        response = self.client.post(
            "/projects/",
            {
                "name": "Invalid commands",
                "repository_url": "https://github.com/example/invalid",
                "runner-setup_commands_mode": "replace",
                "runner-setup_commands": json.dumps(["echo \ud800"]),
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Enter valid Unicode text.")
        self.assertFalse(Project.objects.filter(name="Invalid commands").exists())

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


class ConfiguredProjectMixin:
    def setUp(self):
        super().setUp()
        self.project = Project.objects.create(
            name="Configured", repository_url="https://github.com/example/configured"
        )
        self.github = Credential.objects.create(
            name="Repository",
            provider="github",
            kind="token",
            encrypted_payload="encrypted-reference",
        )
        # Backend credential creation is owned by the following feature branch.
        self.backend = Credential(
            name="Backend",
            provider="codex",
            kind="token",
            encrypted_payload="backend-encrypted-reference",
        )
        Credential.objects.bulk_create([self.backend])
        saved = RunnerSettings.objects.get(pk=1)
        saved.research_backend = "codex"
        saved.implementation_backend = "codex"
        saved.research_model = "research-model"
        saved.implementation_model = "implementation-model"
        saved.research_credential = self.backend
        saved.implementation_credential = self.backend
        saved.issue_credential = self.github
        saved.repository_credential = self.github
        saved.validation_commands = ["python -m unittest"]
        saved.save()


class ConfiguredProjectTestCase(ConfiguredProjectMixin, TestCase):
    pass


class ConfigurationWriteConcurrencyTests(ConfiguredProjectMixin, TransactionTestCase):
    def assert_write_waits_for_snapshot(self, writer):
        started = Event()

        def write_configuration():
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SET statement_timeout = '5s'")
                started.set()
                writer()
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=1) as pool:
            with transaction.atomic():
                revision, snapshot = locked_configuration_snapshot(
                    self.project, configuration_document()["revision"]
                )
                pending = pool.submit(write_configuration)
                self.assertTrue(started.wait(timeout=5))
                with self.assertRaises(TimeoutError):
                    pending.result(timeout=0.2)
                self.assertEqual(configuration_document()["revision"], revision)
            pending.result(timeout=5)
        self.assertNotEqual(configuration_document()["revision"], revision)
        return snapshot

    def test_global_write_waits_for_claim_snapshot(self):
        def writer():
            saved = RunnerSettings.objects.get(pk=1)
            saved.research_model = "future-model"
            saved.save()

        snapshot = self.assert_write_waits_for_snapshot(writer)
        self.assertEqual(snapshot["effective"]["research_model"], "research-model")
        self.assertEqual(
            configuration_document()["global"]["research_model"], "future-model"
        )

    def test_override_write_waits_for_claim_snapshot(self):
        def writer():
            ProjectRunnerSettings.objects.create(
                project=self.project,
                overrides={
                    "research_model": {"mode": "replace", "value": "future-model"}
                },
            )

        snapshot = self.assert_write_waits_for_snapshot(writer)
        self.assertEqual(snapshot["effective"]["research_model"], "research-model")
        effective = effective_project_configuration(self.project)
        self.assertEqual(effective["effective"]["research_model"], "future-model")

    def test_project_write_waits_for_claim_snapshot(self):
        def writer():
            project = Project.objects.get(pk=self.project.pk)
            project.base_branch = "future-branch"
            project.save()

        snapshot = self.assert_write_waits_for_snapshot(writer)
        self.assertEqual(snapshot["base_branch"], "")
        self.assertEqual(
            configuration_document()["projects"][0]["base_branch"], "future-branch"
        )

    def test_credential_rotation_waits_for_claim_snapshot(self):
        def writer():
            credential = Credential.objects.get(pk=self.github.pk)
            credential.encrypted_payload = "future-encrypted-reference"
            credential.save()

        snapshot = self.assert_write_waits_for_snapshot(writer)
        self.assertEqual(snapshot["effective"]["issue_credential_id"], self.github.pk)
        self.assertEqual(
            Credential.objects.get(pk=self.github.pk).encrypted_payload,
            "future-encrypted-reference",
        )


class EffectiveConfigurationTests(ConfiguredProjectTestCase):
    def test_snapshot_preserves_task_settings_after_edits(self):
        with transaction.atomic():
            revision, snapshot = locked_configuration_snapshot(
                self.project, configuration_document()["revision"]
            )
        saved = RunnerSettings.objects.get(pk=1)
        saved.research_model = "next-model"
        saved.paused = True
        saved.save()
        self.assertEqual(snapshot["effective"]["research_model"], "research-model")
        self.assertFalse(snapshot["global"]["paused"])
        self.assertNotEqual(revision, configuration_document()["revision"])
        self.assertTrue(configuration_document()["global"]["paused"])

    def test_inventory_includes_project_only_accounts(self):
        other = Credential(
            name="Project backend",
            provider="claude",
            kind="token",
            encrypted_payload="other-encrypted-reference",
        )
        Credential.objects.bulk_create([other])
        ProjectRunnerSettings.objects.create(
            project=self.project,
            overrides={
                "research_backend": {"mode": "replace", "value": "claude"},
                "research_credential_id": {"mode": "replace", "value": other.pk},
            },
        )
        accounts = configuration_document()["accounts"]
        self.assertIn(
            {
                "backend": "claude",
                "credential_id": other.pk,
                "project_ids": [self.project.pk],
            },
            accounts,
        )
        self.assertIn(
            {
                "backend": "codex",
                "credential_id": self.backend.pk,
                "project_ids": [self.project.pk],
            },
            accounts,
        )

    def test_structural_validity_does_not_establish_execution_readiness(self):
        document = configuration_document()
        self.assertTrue(document["projects"][0]["structurally_valid"])
        self.assertFalse(document["execution_ready"])
        self.assertFalse(document["projects"][0]["execution_ready"])

    def test_missing_override_credential_fails_closed(self):
        other = Credential.objects.create(
            name="Temporary",
            provider="github",
            kind="token",
            encrypted_payload="temporary-encrypted-reference",
        )
        reference = other.pk
        other.delete()
        ProjectRunnerSettings.objects.create(
            project=self.project,
            overrides={"issue_credential_id": {"mode": "replace", "value": reference}},
        )
        errors = effective_project_configuration(self.project)["errors"]
        self.assertTrue(any(error["code"] == "missing_credential" for error in errors))

    def test_unselected_credential_changes_do_not_change_revision(self):
        original = configuration_document()["revision"]
        Credential.objects.create(
            name="Unrelated",
            provider="github",
            kind="token",
            encrypted_payload="unrelated-reference",
        )
        self.assertEqual(original, configuration_document()["revision"])

    def test_credential_rotation_changes_revision_without_secret_serialization(self):
        original = configuration_document()["revision"]
        self.github.encrypted_payload = "rotated-ciphertext"
        self.github.save()
        document = configuration_document()
        self.assertNotEqual(original, document["revision"])
        self.assertNotIn("rotated-ciphertext", str(document))
