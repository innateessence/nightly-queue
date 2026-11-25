from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .forms import APISettingsForm
from .models import APISettings, Credential
from .test_models import TEST_KEY


@override_settings(VIBEARR_CREDENTIAL_KEY=TEST_KEY)
class APISettingsTests(TestCase):
    def setUp(self):
        self.url = reverse("api_settings")
        self.github = self.make_credential("GitHub token", "github", "token")
        self.codex = self.make_credential("Codex login", "codex", "login")

    def make_credential(self, name, provider, kind):
        credential = Credential(name=name, provider=provider, kind=kind)
        secret = (
            "managed-subscription-login"
            if kind == "login"
            else "settings-private-secret"
        )
        credential.set_secret(secret)
        credential.save()
        return credential

    def test_page_shows_only_existing_provider_tokens(self):
        other = self.make_credential("Other account", "claude", "login")
        response = self.client.get(self.url)
        form = response.context["form"]
        self.assertEqual(list(form.fields["github_credential"].queryset), [self.github])
        self.assertNotContains(response, other.name)
        self.assertNotContains(response, "settings-private-secret")
        self.assertNotContains(response, self.github.encrypted_payload)

    def test_save_persists_selections_and_get_restores_them(self):
        data = {
            "github_credential": self.github.pk,
            "auto_sync_interval_seconds": 14400,
        }
        response = self.client.post(self.url, data)
        self.assertRedirects(response, self.url)
        saved = APISettings.objects.get(pk=1)
        self.assertEqual(saved.github_credential, self.github)
        self.assertEqual(saved.auto_sync_interval_seconds, 14400)
        response = self.client.get(self.url)
        self.assertEqual(
            response.context["form"]["github_credential"].value(), self.github.pk
        )
        self.assertEqual(
            response.context["form"]["auto_sync_interval_seconds"].value(), 14400
        )

    def test_sync_interval_choices_and_default(self):
        response = self.client.get(self.url)
        field = response.context["form"]["auto_sync_interval_seconds"]
        self.assertEqual(field.value(), 60)
        self.assertEqual(field.field.min_value, 60)

    def test_supported_sync_intervals_can_be_saved(self):
        for hours in [1, 4, 8, 12, 24]:
            with self.subTest(hours=hours):
                response = self.client.post(
                    self.url, {"auto_sync_interval_seconds": hours * 3600}
                )
                self.assertRedirects(response, self.url)
                self.assertEqual(
                    APISettings.objects.get().auto_sync_interval_seconds, hours * 3600
                )

    def test_invalid_sync_interval_does_not_change_saved_settings(self):
        APISettings.objects.create(
            github_credential=self.github, auto_sync_interval_seconds=28800
        )
        response = self.client.post(self.url, {"auto_sync_interval_seconds": 2})
        self.assertIn("auto_sync_interval_seconds", response.context["form"].errors)
        saved = APISettings.objects.get()
        self.assertEqual(saved.auto_sync_interval_seconds, 28800)
        self.assertEqual(saved.github_credential, self.github)

    def test_sync_interval_is_required(self):
        response = self.client.post(self.url, {})
        self.assertIn("auto_sync_interval_seconds", response.context["form"].errors)
        self.assertFalse(APISettings.objects.exists())

    def test_model_rejects_unsupported_sync_interval(self):
        with self.assertRaises(ValidationError):
            APISettings.objects.create(auto_sync_interval_seconds=2)

    def test_saving_settings_preserves_a_concurrent_sync_timestamp(self):
        saved = APISettings.objects.create()
        data = {"auto_sync_interval_seconds": 14400}
        form = APISettingsForm(data, instance=saved)
        last_sync = timezone.now()
        APISettings.objects.update(last_auto_sync_at=last_sync)
        with patch("credentials.views.APISettingsForm", return_value=form):
            response = self.client.post(self.url, data)
        self.assertRedirects(response, self.url)
        self.assertEqual(APISettings.objects.get().last_auto_sync_at, last_sync)

    def test_wrong_provider_is_rejected(self):
        response = self.client.post(self.url, {"github_credential": self.codex.pk})
        self.assertIn("github_credential", response.context["form"].errors)
        self.assertFalse(APISettings.objects.exists())

    def test_non_token_is_rejected(self):
        credential = self.make_credential("Other account", "claude", "login")
        response = self.client.post(self.url, {"github_credential": credential.pk})
        self.assertIn("github_credential", response.context["form"].errors)
        self.assertFalse(APISettings.objects.exists())

    def test_unknown_credential_is_rejected(self):
        response = self.client.post(self.url, {"github_credential": 9999})
        self.assertIn("github_credential", response.context["form"].errors)

    def test_blank_selection_clears_saved_credential(self):
        APISettings.objects.create(github_credential=self.github)
        self.client.post(self.url, {"auto_sync_interval_seconds": 60})
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
            f'href="{self.url}" aria-current="page"',
        )

    def test_selected_credential_is_protected_from_deletion(self):
        from django.db.models.deletion import ProtectedError

        APISettings.objects.create(github_credential=self.github)
        with self.assertRaises(ProtectedError):
            self.github.delete()
