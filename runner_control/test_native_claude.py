from uuid import uuid4

from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings

from credentials.models import Credential
from credentials.test_models import TEST_KEY
from vibearr_runner.native_claude import CLAUDE_VERSION, claude_profile
from work_queue.services import enqueue_item

from .auth import token_digest
from .configuration import configuration_document
from .models import RunnerIdentity
from .native_services import selected_account
from .secrets import secret_document
from .services import claim_run
from .settings_models import RunnerSettings
from .test_settings import ConfiguredProjectMixin
from .test_support import provision_controller


@override_settings(NQ_CREDENTIAL_KEY=TEST_KEY)
class NativeClaudeSelectionTests(ConfiguredProjectMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.claude = Credential(
            name="Selected Claude API key", provider="claude", kind="token"
        )
        self.claude.set_secret("selected-claude-fixture-key-0123456789")
        self.claude.save()
        settings = RunnerSettings.objects.get(pk=1)
        settings.enabled = True
        settings.implementation_backend = "claude"
        settings.implementation_model = "claude-sonnet-5-5"
        settings.implementation_credential = self.claude
        settings.execution_policy.update(
            runtime_profile="claude-token-v1", network_hosts=["api.anthropic.com"]
        )
        settings.save()
        self.runner = RunnerIdentity.objects.create(
            name="Claude native", token_digest=token_digest("claude-native-fixture")
        )
        provision_controller(self)
        self.item = enqueue_item(
            project=self.project,
            title="Native Claude task",
            instructions="Implement useful work",
        )
        self.run = claim_run(
            runner=self.runner,
            run_id=uuid4(),
            configuration_revision=configuration_document()["revision"],
        )
        self.item.refresh_from_db()

    def grant(self):
        return secret_document(
            {
                "scope": "run",
                "run_id": str(self.run.pk),
                "claim_token": str(self.item.claim_token),
                "purpose": "implementation",
            },
            self.runner,
        )

    def test_claim_freezes_claude_selection_and_only_selected_api_key_is_delivered(
        self,
    ):
        account = selected_account(self.run)
        self.assertEqual(
            account,
            {
                "backend": "claude",
                "credential_id": self.claude.pk,
                "credential_version": self.claude.secret_version,
                "model": "claude-sonnet-5-5",
            },
        )
        grant = self.grant()
        self.assertEqual(grant["credential"]["provider"], "claude")
        self.assertEqual(grant["credential"]["id"], self.claude.pk)
        self.assertEqual(
            grant["material"]["secret"], "selected-claude-fixture-key-0123456789"
        )
        profile = claude_profile("sha256:" + "a" * 64, "token")
        self.assertEqual(
            profile.profile_id,
            self.run.configuration_snapshot["execution_policy"]["runtime_profile"],
        )
        self.assertEqual(CLAUDE_VERSION, "2.1.325")

    def test_changed_project_backend_cannot_reroute_existing_claude_attempt(self):
        settings = RunnerSettings.objects.get(pk=1)
        settings.implementation_backend = "codex"
        settings.implementation_credential = self.backend
        settings.save()
        self.assertEqual(selected_account(self.run)["backend"], "claude")
        with self.assertRaises(ValidationError):
            self.grant()

    def test_claude_run_cannot_fetch_a_second_research_credential(self):
        with self.assertRaises(ValidationError):
            secret_document(
                {
                    "scope": "run",
                    "run_id": str(self.run.pk),
                    "claim_token": str(self.item.claim_token),
                    "purpose": "research",
                },
                self.runner,
            )
