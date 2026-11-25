from django.test import TestCase, override_settings
from django.urls import reverse

from .forms import CredentialForm
from .models import Credential
from .test_models import TEST_KEY


@override_settings(VIBEARR_CREDENTIAL_KEY=TEST_KEY)
class ManagedLoginFormTests(TestCase):
    def test_codex_subscription_reference_requires_no_secret(self):
        form = CredentialForm(
            data={"name": "ChatGPT", "provider": "codex", "kind": "login"}
        )
        self.assertTrue(form.is_valid(), form.errors)
        credential = form.save()
        self.assertEqual(
            credential.get_secret()["secret"], "managed-subscription-login"
        )

    def test_claude_subscription_reference_requires_no_secret(self):
        form = CredentialForm(
            data={"name": "Claude subscription", "provider": "claude", "kind": "login"}
        )
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.save().kind, "login")

    def test_managed_login_metadata_edit_keeps_secret_version(self):
        form = CredentialForm(
            data={"name": "ChatGPT", "provider": "codex", "kind": "login"}
        )
        self.assertTrue(form.is_valid(), form.errors)
        credential = form.save()
        edit = CredentialForm(
            instance=credential,
            data={"name": "Renamed", "provider": "codex", "kind": "login"},
        )
        self.assertTrue(edit.is_valid(), edit.errors)
        self.assertEqual(edit.save().secret_version, 1)

    def test_login_help_uses_selected_provider_and_id(self):
        form = CredentialForm(
            data={"name": "ChatGPT", "provider": "codex", "kind": "login"}
        )
        self.assertTrue(form.is_valid(), form.errors)
        credential = form.save()
        response = self.client.get(reverse("credential_edit", args=[credential.pk]))
        self.assertContains(response, f"login codex --credential-id {credential.pk}")
        self.assertContains(response, "No model API key is required")
        self.assertNotContains(response, "managed-subscription-login")

    def test_repository_provider_cannot_use_managed_model_login(self):
        form = CredentialForm(
            data={"name": "Invalid", "provider": "github", "kind": "login"}
        )
        self.assertFalse(form.is_valid())
        self.assertFalse(Credential.objects.exists())
