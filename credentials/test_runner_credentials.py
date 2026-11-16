import json

from django.core.exceptions import ValidationError
from django.db.models.deletion import ProtectedError
from django.test import TestCase, override_settings
from django.urls import reverse

from projects.models import Project
from runner_control.settings_models import ProjectRunnerSettings, RunnerSettings

from .forms import CredentialForm
from .models import Credential, CredentialSecretVersion
from .test_models import TEST_KEY


@override_settings(NQ_CREDENTIAL_KEY=TEST_KEY)
class BackendCredentialTests(TestCase):
    def make_credential(
        self, provider="codex", kind="token", secret="backend-private-token"
    ):
        credential = Credential(name="Backend", provider=provider, kind=kind)
        credential.set_secret(secret)
        credential.save()
        return credential

    def test_codex_session_round_trip(self):
        material = {
            "tokens": {
                "access_token": "access",
                "refresh_token": "refresh",
                "id_token": "id",
                "account_id": "account",
            }
        }
        secret = json.dumps(material)
        credential = self.make_credential(kind="session", secret=secret)
        credential.refresh_from_db()
        self.assertEqual(json.loads(credential.get_secret()["secret"]), material)
        self.assertNotIn("access", credential.encrypted_payload)

    def test_codex_session_rejects_unknown_fields_without_echo(self):
        secret = '{"private-secret-value":"other"}'
        with self.assertRaises(ValidationError) as caught:
            self.make_credential(kind="session", secret=secret)
        self.assertNotIn("private-secret-value", str(caught.exception))

    def test_opencode_ollama_connection(self):
        secret = '{"provider":"ollama","base_url":"http://runner-ollama:11434/api"}'
        credential = self.make_credential(
            provider="opencode", kind="connection", secret=secret
        )
        self.assertEqual(credential.get_secret()["secret"], secret)

    def test_connection_rejects_url_embedded_password(self):
        secret = (
            '{"provider":"ollama","base_url":"http://user:private-password@host/v1"}'
        )
        with self.assertRaises(ValidationError) as caught:
            self.make_credential(provider="opencode", kind="connection", secret=secret)
        self.assertNotIn("private-password", str(caught.exception))

    def test_wrong_kind_rejected(self):
        with self.assertRaises(ValidationError):
            self.make_credential(provider="claude", kind="session", secret="{}")

    def test_provider_cannot_change(self):
        credential = self.make_credential()
        credential.provider = "claude"
        with self.assertRaises(ValidationError):
            credential.save()

    def test_rotation_increments_version(self):
        credential = self.make_credential()
        credential.set_secret("rotated-secret")
        credential.save(update_fields=["encrypted_payload"])
        credential.refresh_from_db()
        self.assertEqual(credential.secret_version, 2)
        self.assertEqual(credential.get_secret()["secret"], "rotated-secret")

    def test_stale_metadata_save_preserves_rotation(self):
        credential = self.make_credential()
        stale = Credential.objects.get(pk=credential.pk)
        credential.set_secret("rotated-secret")
        credential.save()
        stale.name = "Renamed"
        stale.save()
        stale.refresh_from_db()
        self.assertEqual(stale.get_secret()["secret"], "rotated-secret")
        self.assertEqual(stale.secret_version, 2)

    def test_stale_partial_metadata_save_preserves_rotation_version(self):
        credential = self.make_credential()
        stale = Credential.objects.get(pk=credential.pk)
        credential.set_secret("rotated-secret")
        credential.save()
        stale.name = "Renamed"
        stale.save(update_fields=["name"])
        stale.refresh_from_db()
        self.assertEqual(stale.get_secret()["secret"], "rotated-secret")
        self.assertEqual(stale.secret_version, 2)

    def test_refreshed_metadata_save_preserves_later_rotation(self):
        credential = self.make_credential()
        stale = Credential.objects.get(pk=credential.pk)
        credential.set_secret("first-rotation")
        credential.save()
        stale.refresh_from_db()
        credential.set_secret("second-rotation")
        credential.save()
        stale.name = "Renamed"
        stale.save()
        self.assertEqual(stale.get_secret()["secret"], "second-rotation")
        self.assertEqual(stale.secret_version, 3)

    def test_rotation_retains_only_encrypted_old_material(self):
        credential = self.make_credential()
        ciphertext = credential.encrypted_payload
        credential.set_secret("rotated-secret")
        credential.save()
        version = CredentialSecretVersion.objects.get(credential=credential)
        self.assertEqual(version.version, 1)
        self.assertEqual(version.encrypted_payload, ciphertext)
        self.assertNotIn("backend-private-token", version.encrypted_payload)
        self.assertNotIn("backend-private-token", str(version))

    def test_retained_version_protects_credential_deletion(self):
        credential = self.make_credential()
        credential.set_secret("rotated-secret")
        credential.save()
        with self.assertRaises(ProtectedError):
            credential.delete()

    def test_admin_reports_retained_versions_before_deletion(self):
        from django.contrib.auth import get_user_model

        self.client.force_login(
            get_user_model().objects.create_superuser("reviewer", password="test")
        )
        credential = self.make_credential()
        credential.set_secret("rotated-secret")
        credential.save()
        response = self.client.post(
            reverse("admin:credentials_credential_delete", args=[credential.pk]),
            {"post": "yes"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Cannot delete")
        self.assertTrue(Credential.objects.filter(pk=credential.pk).exists())
        self.assertNotContains(response, "backend-private-token")
        self.assertNotContains(response, credential.encrypted_payload)

    def test_rename_preserves_version(self):
        credential = self.make_credential()
        credential.name = "Renamed"
        credential.save()
        self.assertEqual(credential.secret_version, 1)

    def test_stale_save_cannot_unrevoke(self):
        credential = self.make_credential()
        stale = Credential.objects.get(pk=credential.pk)
        credential.revoke()
        stale.name = "Renamed"
        stale.save()
        stale.refresh_from_db()
        self.assertIsNotNone(stale.revoked_at)

    def test_selected_global_reference_is_protected(self):
        credential = self.make_credential()
        RunnerSettings.objects.update_or_create(
            pk=1, defaults={"research_credential": credential}
        )
        with self.assertRaises(ProtectedError):
            credential.delete()

    def test_selected_project_json_reference_is_protected_on_bulk_delete(self):
        credential = self.make_credential()
        project = Project.objects.create(
            name="Project", repository_url="https://github.com/example/project"
        )
        ProjectRunnerSettings.objects.create(
            project=project,
            overrides={
                "research_credential_id": {"mode": "replace", "value": credential.pk}
            },
        )
        with self.assertRaises(ProtectedError):
            Credential.objects.filter(pk=credential.pk).delete()

    def test_admin_reports_project_override_protection_before_deletion(self):
        from django.contrib.auth import get_user_model

        self.client.force_login(
            get_user_model().objects.create_superuser("reviewer", password="test")
        )
        credential = self.make_credential()
        project = Project.objects.create(
            name="Protected project",
            repository_url="https://github.com/example/project",
        )
        ProjectRunnerSettings.objects.create(
            project=project,
            overrides={
                "research_credential_id": {"mode": "replace", "value": credential.pk}
            },
        )
        response = self.client.post(
            reverse("admin:credentials_credential_delete", args=[credential.pk]),
            {"post": "yes"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Cannot delete")
        self.assertContains(response, "Protected project")
        self.assertTrue(Credential.objects.filter(pk=credential.pk).exists())

    def test_backend_form_sets_provider_before_validating_secret(self):
        form = CredentialForm(
            {
                "name": "Backend",
                "provider": "claude",
                "kind": "token",
                "secret": "private-token",
            }
        )
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.save().provider, "claude")

    def test_edit_does_not_read_back_secret(self):
        credential = self.make_credential()
        response = self.client.get(reverse("credential_edit", args=[credential.pk]))
        self.assertContains(response, "Backend")
        self.assertNotContains(response, "backend-private-token")
        self.assertNotContains(response, credential.encrypted_payload)

    def test_revocation_requires_post_and_csrf(self):
        credential = self.make_credential()
        url = reverse("credential_revoke", args=[credential.pk])
        self.assertEqual(self.client.get(url).status_code, 405)
        from django.test import Client

        self.assertEqual(Client(enforce_csrf_checks=True).post(url).status_code, 403)
        self.assertEqual(self.client.post(url).status_code, 302)
        credential.refresh_from_db()
        self.assertIsNotNone(credential.revoked_at)
