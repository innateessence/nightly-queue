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
            implementation_backend="claude", implementation_model="claude-sonnet-5-5"
        )
        project = Project.objects.create(
            name="Claude", repository_url="https://github.com/example/repo"
        )
        ProjectRunnerSettings.objects.create(
            project=project,
            overrides={
                "implementation_model": {
                    "mode": "replace",
                    "value": "https://untrusted/model",
                }
            },
        )
        document = configuration_document()
        errors = document["projects"][0]["errors"]
        self.assertTrue(
            any(
                error["field"] == "implementation_model"
                and error["code"] == "invalid_model"
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
            self.assertIn("Native adapters", form.fields[f"{stage}_model"].help_text)

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


class ClaudeReleaseContractTests(TestCase):
    def entries(self):
        from coding_runner.backend_transports import TRANSPORTS

        return [
            {
                "backend": provider,
                "credential_id": index,
                "model": model,
                "secret_version": 1,
                "transport": TRANSPORTS[provider],
            }
            for provider, index, model in (
                ("codex", 7, "web-model"),
                ("claude", 13, "claude-sonnet-5-5"),
                ("claude", 14, "claude-opus-5-5"),
            )
        ]

    def test_live_evidence_requires_each_selected_account_model_and_transport(self):
        from .release import verify_model_responses

        entries = self.entries()
        rows = [{**entry, "response_id": "fixture-response"} for entry in entries]
        verify_model_responses(rows, entries)

    def test_codex_transport_cannot_qualify_claude_account(self):
        from .release import verify_model_responses
        from .services import RunConflict

        entries = self.entries()
        rows = [{**entry, "response_id": "fixture-response"} for entry in entries]
        rows[1]["transport"] = rows[0]["transport"]
        with self.assertRaises(RunConflict):
            verify_model_responses(rows, entries)

    def test_missing_distinct_claude_account_blocks_qualification(self):
        from .release import verify_model_responses
        from .services import RunConflict

        entries = self.entries()
        rows = [{**entry, "response_id": "fixture-response"} for entry in entries[:2]]
        with self.assertRaises(RunConflict):
            verify_model_responses(rows, entries)

    def test_old_secret_version_cannot_qualify_rotated_account(self):
        from .release import verify_model_responses
        from .services import RunConflict

        entries = self.entries()
        rows = [{**entry, "response_id": "fixture-response"} for entry in entries]
        rows[1]["secret_version"] = 0
        with self.assertRaises(RunConflict):
            verify_model_responses(rows, entries)
