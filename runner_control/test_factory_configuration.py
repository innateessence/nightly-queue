import json

from django.core.exceptions import ValidationError
from django.db import transaction

from .configuration import (
    configuration_document,
    effective_project_configuration,
    locked_configuration_snapshot,
)
from .factory_configuration import effective_factory_configuration
from .settings_forms import ProjectRunnerSettingsForm
from .settings_models import ProjectRunnerSettings, RunnerSettings
from .test_settings import ConfiguredProjectTestCase


class FactoryConfigurationTests(ConfiguredProjectTestCase):
    def save_factory(self, value):
        saved, _ = ProjectRunnerSettings.objects.get_or_create(project=self.project)
        saved.overrides = {"factory": {"mode": "replace", "value": value}}
        saved.save()
        return saved

    def test_old_configuration_preserves_disabled_factory_defaults(self):
        document = configuration_document()
        self.assertFalse(document["global"]["factory"]["enabled"])
        self.assertFalse(document["projects"][0]["effective"]["factory"]["enabled"])
        self.assertTrue(document["projects"][0]["structurally_valid"])

    def test_effective_roles_inherit_selected_model_and_instructions(self):
        self.save_factory({"schema_version": 1, "enabled": True})
        effective = effective_project_configuration(self.project)["effective"]
        for role in effective["factory"]["roles"].values():
            self.assertEqual(role["model"], effective["implementation_model"])
            self.assertEqual(
                role["instructions"], effective["implementation_instructions"]
            )

    def test_role_models_and_instructions_can_be_overridden(self):
        self.save_factory(
            {
                "roles": {
                    "reviewer-code": {
                        "model": "gpt-5.5",
                        "instructions": "Inspect scope.",
                    }
                }
            }
        )
        settings = effective_project_configuration(self.project)["effective"]["factory"]
        self.assertEqual(
            settings["roles"]["reviewer-code"],
            {"model": "gpt-5.5", "instructions": "Inspect scope."},
        )

    def test_role_cannot_switch_to_another_credential(self):
        with self.assertRaises(ValidationError):
            self.save_factory({"roles": {"build": {"credential_id": self.github.pk}}})

    def test_role_model_must_match_selected_provider(self):
        self.save_factory(
            {"enabled": True, "roles": {"build": {"model": "claude-sonnet-4-6"}}}
        )
        project = effective_project_configuration(self.project)
        self.assertFalse(project["structurally_valid"])
        self.assertEqual(project["errors"][0]["field"], "factory")

    def test_model_url_is_rejected_before_configuration_save(self):
        with self.assertRaisesMessage(ValidationError, "model IDs"):
            self.save_factory(
                {"roles": {"build": {"model": "https://untrusted.example/model"}}}
            )

    def test_role_cannot_expand_runtime_tool_permissions(self):
        with self.assertRaises(ValidationError):
            self.save_factory({"roles": {"build": {"tools": ["shell"]}}})

    def test_factory_override_cannot_be_cleared_into_invalid_settings(self):
        with self.assertRaises(ValidationError):
            ProjectRunnerSettings(
                project=self.project, overrides={"factory": {"mode": "clear"}}
            ).save()

    def test_project_override_form_accepts_structured_factory_settings(self):
        form = ProjectRunnerSettingsForm(
            {
                "factory_mode": "replace",
                "factory": json.dumps(
                    {"enabled": True, "roles": {"build": {"model": "gpt-5.5"}}}
                ),
            }
        )
        self.assertTrue(form.is_valid(), form.errors)
        form.save(self.project)
        self.assertTrue(
            effective_project_configuration(self.project)["effective"]["factory"][
                "enabled"
            ]
        )

    def test_factory_source_renders_only_supported_choices(self):
        form = ProjectRunnerSettingsForm()
        rendered = str(form["factory_mode"])
        self.assertIn('value="inherit"', rendered)
        self.assertIn('value="replace"', rendered)
        self.assertNotIn('value="clear"', rendered)
        self.assertIn('{"enabled": false}', form["factory"].help_text)

    def test_other_override_sources_retain_clear_value(self):
        form = ProjectRunnerSettingsForm({"setup_commands_mode": "clear"})
        self.assertIn('value="clear"', str(form["setup_commands_mode"]))
        self.assertTrue(form.is_valid(), form.errors)
        saved = form.save(self.project)
        self.assertEqual(saved.overrides["setup_commands"], {"mode": "clear"})

    def test_factory_clear_source_is_rejected_by_the_form(self):
        form = ProjectRunnerSettingsForm({"factory_mode": "clear"})
        self.assertFalse(form.is_valid())
        self.assertIn("factory_mode", form.errors)
        self.assertFalse(ProjectRunnerSettings.objects.exists())

    def test_factory_disable_value_survives_a_form_roundtrip(self):
        self.save_factory({"enabled": True})
        form = ProjectRunnerSettingsForm(
            {"factory_mode": "replace", "factory": '{"enabled": false}'}
        )
        self.assertTrue(form.is_valid(), form.errors)
        saved = form.save(self.project)
        restored = ProjectRunnerSettingsForm(instance=saved)
        self.assertEqual(restored["factory_mode"].value(), "replace")
        self.assertEqual(restored.initial["factory"], {"enabled": False})
        self.assertFalse(
            effective_project_configuration(self.project)["effective"]["factory"][
                "enabled"
            ]
        )

    def test_factory_inherit_source_survives_a_form_roundtrip(self):
        self.save_factory({"enabled": True})
        form = ProjectRunnerSettingsForm({"factory_mode": "inherit"})
        self.assertTrue(form.is_valid(), form.errors)
        saved = form.save(self.project)
        restored = ProjectRunnerSettingsForm(instance=saved)
        self.assertEqual(restored["factory_mode"].value(), "inherit")
        self.assertEqual(saved.overrides["factory"], {"mode": "inherit"})

    def test_empty_factory_replacement_explains_supported_options(self):
        form = ProjectRunnerSettingsForm({"factory_mode": "replace", "factory": ""})
        self.assertFalse(form.is_valid())
        self.assertIn("Inherit runner settings", form.errors["factory"][0])
        self.assertIn('{"enabled": false}', form.errors["factory"][0])
        self.assertNotIn("Clear value", form.errors["factory"][0])

    def test_factory_change_invalidates_configuration_revision(self):
        original = configuration_document()["revision"]
        self.save_factory({"enabled": True})
        self.assertNotEqual(configuration_document()["revision"], original)

    def test_claim_snapshot_keeps_original_factory_role_configuration(self):
        self.save_factory({"enabled": True, "roles": {"build": {"model": "gpt-5.5"}}})
        revision = configuration_document()["revision"]
        with transaction.atomic():
            _, snapshot = locked_configuration_snapshot(self.project, revision)
        self.save_factory({"enabled": True, "roles": {"build": {"model": "gpt-5.6"}}})
        self.assertEqual(
            snapshot["effective"]["factory"]["roles"]["build"]["model"], "gpt-5.5"
        )
        with transaction.atomic(), self.assertRaises(ValidationError):
            locked_configuration_snapshot(self.project, revision)

    def test_factory_roles_reuse_the_selected_account_inventory(self):
        self.save_factory({"enabled": True})
        document = configuration_document()
        selected = [
            account
            for account in document["accounts"]
            if self.project.pk in account["project_ids"]
        ]
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]["credential_id"], self.backend.pk)

    def test_factory_repairs_cannot_exceed_existing_execution_policy(self):
        saved = RunnerSettings.objects.get(pk=1)
        saved.execution_policy["repair_limit"] = 1
        saved.save()
        settings = effective_project_configuration(self.project)["effective"]["factory"]
        self.assertEqual(settings["max_repairs"], 1)

    def test_corrupt_factory_override_blocks_instead_of_crashing(self):
        self.save_factory({"enabled": False})
        ProjectRunnerSettings.objects.filter(project=self.project).update(
            overrides={"factory": {"mode": "replace", "value": []}}
        )
        project = effective_project_configuration(self.project)
        self.assertFalse(project["structurally_valid"])

    def test_enabling_factory_requests_qualified_worker_roles(self):
        self.save_factory({"enabled": True})
        project = effective_project_configuration(self.project)
        self.assertIn("factory_roles", project["required_capabilities"])

    def test_disabled_empty_backend_defaults_do_not_crash(self):
        effective = configuration_document()["global"]
        effective["implementation_backend"] = ""
        effective["implementation_model"] = ""
        self.assertFalse(effective_factory_configuration({}, effective)["enabled"])
