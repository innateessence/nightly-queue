import json

from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from django.urls import reverse

from .forms import CredentialForm
from .models import Credential
from .test_models import TEST_KEY
from runner_control.validators import validate_backend_model


@override_settings(NQ_CREDENTIAL_KEY=TEST_KEY)
class OpenCodeConnectionTests(TestCase):
    def data(self, **values):
        return {
            "name": "Local inference",
            "provider": "opencode",
            "kind": "connection",
            "connection_provider": "ollama",
            "connection_endpoint": "http://runner-ollama:11434",
            **values,
        }

    def test_create_local_connection_without_internal_json(self):
        form = CredentialForm(data=self.data())
        self.assertTrue(form.is_valid(), form.errors)
        credential = form.save()
        self.assertEqual(
            json.loads(credential.get_secret()["secret"]),
            {"provider": "ollama", "base_url": "http://runner-ollama:11434"},
        )

    def test_create_hosted_connection_with_fixed_endpoint(self):
        form = CredentialForm(
            data=self.data(
                connection_provider="openai",
                connection_endpoint="",
                connection_api_key="private-api-key",
            )
        )
        self.assertTrue(form.is_valid(), form.errors)
        credential = form.save()
        self.assertEqual(
            json.loads(credential.get_secret()["secret"])["api_key"], "private-api-key"
        )

    def test_hosted_connection_rejects_arbitrary_endpoint(self):
        form = CredentialForm(
            data=self.data(
                connection_provider="openai",
                connection_endpoint="https://steal.example/v1",
                connection_api_key="private-api-key",
            )
        )
        self.assertFalse(form.is_valid())
        self.assertFalse(Credential.objects.exists())

    def test_wrong_credential_provider_rejects_connection_fields(self):
        form = CredentialForm(
            data=self.data(provider="github", kind="token", secret="token")
        )
        self.assertFalse(form.is_valid())

    def test_json_and_fields_cannot_conflict(self):
        form = CredentialForm(
            data=self.data(secret='{"provider":"openai","api_key":"secret"}')
        )
        self.assertFalse(form.is_valid())

    def test_secret_is_not_read_back_on_validation_error(self):
        response = self.client.post(
            reverse("credential_list"),
            self.data(
                name="",
                connection_provider="openai",
                connection_api_key="private-api-key",
                connection_endpoint="",
            ),
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "private-api-key")

    def test_edit_without_fields_keeps_saved_secret_and_version(self):
        form = CredentialForm(data=self.data())
        self.assertTrue(form.is_valid(), form.errors)
        original = form.save()
        edit = CredentialForm(
            instance=original,
            data={"name": "New name", "provider": "opencode", "kind": "connection"},
        )
        self.assertTrue(edit.is_valid(), edit.errors)
        updated = edit.save()
        self.assertEqual(updated.secret_version, 1)
        self.assertIn("runner-ollama", updated.get_secret()["secret"])

    def test_rotation_increments_secret_version(self):
        form = CredentialForm(data=self.data())
        self.assertTrue(form.is_valid(), form.errors)
        original = form.save()
        replacement = CredentialForm(
            instance=original,
            data=self.data(connection_endpoint="http://127.0.0.1:11435"),
        )
        self.assertTrue(replacement.is_valid(), replacement.errors)
        self.assertEqual(replacement.save().secret_version, 2)

    def test_model_validation_rejects_endpoint_or_command(self):
        for value in ("https://host/model", "qwen3 --auto", "../secret"):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                validate_backend_model("opencode", value)
        validate_backend_model("opencode", "qwen3-coder:30b")

    def test_connection_help_is_visible(self):
        response = self.client.get(reverse("credential_list"))
        self.assertContains(response, "Ollama endpoint")
        self.assertContains(response, "Native OpenCode CLI execution is unsupported")
