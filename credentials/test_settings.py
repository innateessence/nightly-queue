from django.test import Client, TestCase, override_settings
from django.urls import reverse

from .models import APISettings, Credential
from .test_models import TEST_KEY


@override_settings(NQ_CREDENTIAL_KEY=TEST_KEY)
class APISettingsTests(TestCase):
    def setUp(self):
        self.url = reverse("api_settings")
        self.github = self.make_credential("GitHub token", "github", "token")
        self.gitlab = self.make_credential("GitLab token", "gitlab", "token")

    def make_credential(self, name, provider, kind):
        credential = Credential(name=name, provider=provider, kind=kind)
        credential.set_secret("settings-private-secret")
        credential.save()
        return credential

    def test_page_shows_only_existing_provider_tokens(self):
        other = self.make_credential("Other secret", "github", "secret")
        response = self.client.get(self.url)
        form = response.context["form"]
        self.assertEqual(list(form.fields["github_credential"].queryset), [self.github])
        self.assertEqual(list(form.fields["gitlab_credential"].queryset), [self.gitlab])
        self.assertNotContains(response, other.name)
        self.assertNotContains(response, "settings-private-secret")
        self.assertNotContains(response, self.github.encrypted_payload)

    def test_save_persists_selections_and_get_restores_them(self):
        data = {
            "github_credential": self.github.pk,
            "gitlab_credential": self.gitlab.pk,
        }
        response = self.client.post(self.url, data)
        self.assertRedirects(response, self.url)
        saved = APISettings.objects.get(pk=1)
        self.assertEqual(saved.github_credential, self.github)
        self.assertEqual(saved.gitlab_credential, self.gitlab)
        response = self.client.get(self.url)
        self.assertEqual(
            response.context["form"]["github_credential"].value(), self.github.pk
        )

    def test_wrong_provider_is_rejected(self):
        response = self.client.post(self.url, {"github_credential": self.gitlab.pk})
        self.assertIn("github_credential", response.context["form"].errors)
        self.assertFalse(APISettings.objects.exists())

    def test_non_token_is_rejected(self):
        credential = self.make_credential("Other secret", "github", "secret")
        response = self.client.post(self.url, {"github_credential": credential.pk})
        self.assertIn("github_credential", response.context["form"].errors)
        self.assertFalse(APISettings.objects.exists())

    def test_unknown_credential_is_rejected(self):
        response = self.client.post(self.url, {"github_credential": 9999})
        self.assertIn("github_credential", response.context["form"].errors)

    def test_blank_selection_clears_saved_credential(self):
        APISettings.objects.create(github_credential=self.github)
        self.client.post(self.url, {})
        self.assertIsNone(APISettings.objects.get().github_credential)

    def test_post_requires_csrf(self):
        response = Client(enforce_csrf_checks=True).post(self.url, {})
        self.assertEqual(response.status_code, 403)
        self.assertFalse(APISettings.objects.exists())

    def test_get_does_not_create_settings(self):
        self.client.get(self.url)
        self.assertFalse(APISettings.objects.exists())

    def test_navigation_links_to_settings(self):
        response = self.client.get(self.url)
        self.assertContains(
            response,
            f'<a href="{self.url}" aria-current="page">Settings</a>',
            html=True,
        )

    def test_selected_credential_is_protected_from_deletion(self):
        from django.db.models.deletion import ProtectedError

        APISettings.objects.create(github_credential=self.github)
        with self.assertRaises(ProtectedError):
            self.github.delete()
