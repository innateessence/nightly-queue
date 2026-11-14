from django.contrib.admin.models import LogEntry
from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from projects.models import Project

from .forms import CredentialForm
from .models import Credential
from .test_models import TEST_KEY, private_key


@override_settings(NQ_CREDENTIAL_KEY=TEST_KEY, NQ_API_TOKEN="api-token")
class CredentialAdminTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_superuser(
            username="admin", password="admin-password"
        )
        self.client.force_login(self.user)
        self.url = reverse("admin:credentials_credential_add")
        self.data = {
            "name": "GitHub access",
            "kind": "github_token",
            "secret": "private-token",
            "passphrase": "",
        }

    def make_credential(self):
        self.client.post(self.url, self.data)
        return Credential.objects.get()

    def change_url(self, credential):
        return reverse("admin:credentials_credential_change", args=[credential.pk])

    def test_anonymous_users_cannot_create_credentials(self):
        self.client.logout()
        response = self.client.post(self.url, self.data)
        self.assertEqual(response.status_code, 302)
        self.assertFalse(Credential.objects.exists())

    def test_staff_without_permission_cannot_create_credentials(self):
        user = get_user_model().objects.create_user(username="staff", is_staff=True)
        self.client.force_login(user)
        response = self.client.post(self.url, self.data)
        self.assertEqual(response.status_code, 403)
        self.assertFalse(Credential.objects.exists())

    def test_admin_can_create_credential(self):
        response = self.client.post(self.url, self.data)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            Credential.objects.get().get_secret()["secret"], "private-token"
        )

    def test_creation_requires_secret(self):
        self.data["secret"] = ""
        response = self.client.post(self.url, self.data)
        self.assertContains(response, "Enter a token or private key.")
        self.assertFalse(Credential.objects.exists())

    def test_admin_can_create_encrypted_ssh_key(self):
        key = private_key("key-password")
        self.data.update(kind="ssh_key", secret=key, passphrase="key-password")
        response = self.client.post(self.url, self.data)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            Credential.objects.get().get_secret(),
            {"secret": key, "passphrase": "key-password"},
        )

    def test_saved_secret_is_not_rendered(self):
        credential = self.make_credential()
        response = self.client.get(self.change_url(credential))
        self.assertNotContains(response, "private-token")
        self.assertNotContains(response, credential.encrypted_payload)

    def test_invalid_form_does_not_echo_secret(self):
        self.data["name"] = ""
        response = self.client.post(self.url, self.data)
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "private-token")

    def test_invalid_form_does_not_echo_passphrase(self):
        self.data["passphrase"] = "private-passphrase"
        response = self.client.post(self.url, self.data)
        self.assertNotContains(response, "private-passphrase")

    def test_blank_edit_keeps_existing_secret(self):
        credential = self.make_credential()
        ciphertext = credential.encrypted_payload
        self.data.update(name="Renamed", secret="")
        response = self.client.post(self.change_url(credential), self.data)
        self.assertEqual(response.status_code, 302)
        credential.refresh_from_db()
        self.assertEqual(credential.name, "Renamed")
        self.assertEqual(credential.encrypted_payload, ciphertext)

    def test_edit_can_replace_secret(self):
        credential = self.make_credential()
        self.data["secret"] = "replacement-token"
        response = self.client.post(self.change_url(credential), self.data)
        self.assertEqual(response.status_code, 302)
        credential.refresh_from_db()
        self.assertEqual(credential.get_secret()["secret"], "replacement-token")

    def test_failed_edit_preserves_saved_secret(self):
        credential = self.make_credential()
        self.data.update(name="", secret="replacement-token")
        self.client.post(self.change_url(credential), self.data)
        credential.refresh_from_db()
        self.assertEqual(credential.get_secret()["secret"], "private-token")

    def test_edit_cannot_change_type(self):
        credential = self.make_credential()
        self.data.update(kind="ssh_key", secret="")
        self.client.post(self.change_url(credential), self.data)
        credential.refresh_from_db()
        self.assertEqual(credential.kind, "github_token")

    def test_passphrase_change_requires_private_key(self):
        credential = self.make_credential()
        self.data.update(secret="", passphrase="password")
        form = CredentialForm(self.data, instance=credential)
        self.assertFalse(form.is_valid())
        self.assertIn("passphrase", form.errors)

    @override_settings(NQ_CREDENTIAL_KEY="")
    def test_missing_key_shows_configuration_error(self):
        response = self.client.post(self.url, self.data)
        self.assertContains(response, "Configure NQ_CREDENTIAL_KEY")
        self.assertFalse(Credential.objects.exists())

    def test_admin_log_does_not_contain_secret(self):
        self.make_credential()
        entry = LogEntry.objects.get()
        self.assertNotIn("private-token", entry.change_message)
        self.assertNotIn("private-token", entry.object_repr)

    def test_creation_requires_csrf(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)
        response = client.post(self.url, self.data)
        self.assertEqual(response.status_code, 403)
        self.assertFalse(Credential.objects.exists())

    def test_project_api_does_not_expose_credential(self):
        credential = self.make_credential()
        Project.objects.create(
            name="Example",
            repository_url="https://github.com/example/repo",
            credential=credential,
        )
        response = self.client.get(
            reverse("api_projects"), HTTP_AUTHORIZATION="Bearer api-token"
        )
        self.assertNotContains(response, "private-token")
        self.assertNotContains(response, credential.encrypted_payload)
        self.assertNotIn("credential", response.json()["projects"][0])

    def test_public_project_form_cannot_assign_credential(self):
        credential = self.make_credential()
        data = {
            "name": "Example",
            "repository_url": "https://github.com/example/repo",
            "credential": credential.pk,
        }
        response = Client().post(reverse("project_list"), data)
        self.assertEqual(response.status_code, 302)
        self.assertIsNone(Project.objects.get().credential_id)

    def test_public_project_edit_preserves_admin_credential(self):
        credential = self.make_credential()
        project = Project.objects.create(
            name="Example",
            repository_url="https://github.com/example/repo",
            credential=credential,
        )
        data = {
            "name": "Renamed",
            "repository_url": project.repository_url,
            "credential": "",
        }
        Client().post(reverse("edit_project", args=[project.pk]), data)
        project.refresh_from_db()
        self.assertEqual(project.credential_id, credential.pk)
