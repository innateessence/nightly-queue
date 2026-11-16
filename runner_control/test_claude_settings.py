from django.core.exceptions import ValidationError
from django.test import TestCase

from credentials.models import Credential
from projects.models import Project

from .configuration import configuration_document
from .settings_forms import RunnerSettingsForm, ProjectRunnerSettingsForm
from .settings_models import RunnerSettings, ProjectRunnerSettings


class ClaudeSettingsTests(TestCase):
    def test_global_model_accepts_claude_api_identifier(self):
        settings = RunnerSettings(
            research_backend="claude", research_model="claude-sonnet-5-5"
        )
        settings.full_clean(validate_unique=False)

    def test_global_model_rejects_endpoint_and_cli_alias(self):
        for model in (
            "https://example.com/model",
            "sonnet",
            "claude-X",
            "claude-x\nheader",
        ):
            with self.subTest(model=model), self.assertRaises(ValidationError):
                RunnerSettings(
                    research_backend="claude", research_model=model
                ).full_clean(validate_unique=False)

    def test_each_stage_validates_its_own_provider_model(self):
        settings = RunnerSettings(
            research_backend="codex",
            research_model="web-model",
            implementation_backend="claude",
            implementation_model="sonnet",
        )
        with self.assertRaises(ValidationError) as failure:
            settings.full_clean(validate_unique=False)
        self.assertIn("implementation_model", failure.exception.message_dict)
        self.assertNotIn("research_model", failure.exception.message_dict)

    def test_incomplete_settings_remain_editable(self):
        RunnerSettings(research_backend="claude").full_clean(validate_unique=False)

    def test_project_inherited_claude_backend_rejects_bad_model(self):
        RunnerSettings.objects.update(
            research_backend="claude", research_model="claude-sonnet-5-5"
        )
        project = Project.objects.create(
            name="Claude", repository_url="https://github.com/example/repo"
        )
        ProjectRunnerSettings.objects.create(
            project=project,
            overrides={
                "research_model": {
                    "mode": "replace",
                    "value": "https://untrusted/model",
                }
            },
        )
        document = configuration_document()
        errors = document["projects"][0]["errors"]
        self.assertTrue(
            any(
                error["field"] == "research_model" and error["code"] == "invalid_model"
                for error in errors
            )
        )
        self.assertFalse(document["projects"][0]["structurally_valid"])

    def test_model_choices_are_web_managed_for_both_stages(self):
        form = RunnerSettingsForm()
        for stage in ("research", "implementation"):
            self.assertEqual(
                form.fields[f"{stage}_model"].widget.attrs["list"],
                "runner-claude-models",
            )
            self.assertIn("Messages API", form.fields[f"{stage}_model"].help_text)

    def test_project_model_choices_are_independent(self):
        form = ProjectRunnerSettingsForm()
        self.assertEqual(
            form.fields["research_model"].widget.attrs["list"], "runner-claude-models"
        )
        self.assertEqual(
            form.fields["implementation_model"].widget.attrs["list"],
            "runner-claude-models",
        )

    def test_claude_token_choice_exists_for_each_stage(self):
        from cryptography.fernet import Fernet

        with self.settings(NQ_CREDENTIAL_KEY=Fernet.generate_key().decode()):
            credential = Credential(name="Claude API", provider="claude", kind="token")
            credential.set_secret("web-managed-claude-token")
            credential.save()
        form = RunnerSettingsForm()
        self.assertIn(credential, form.fields["research_credential"].queryset)
        self.assertIn(credential, form.fields["implementation_credential"].queryset)
