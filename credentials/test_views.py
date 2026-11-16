from django.test import Client, TestCase, override_settings
from django.urls import reverse

from .models import Credential
from .test_models import TEST_KEY, private_key


@override_settings(NQ_CREDENTIAL_KEY=TEST_KEY)
class CredentialViewTests(TestCase):
    def setUp(self):
        self.url = reverse("credential_list")
        self.data = {
            "name": "GitHub work account",
            "provider": Credential.Provider.GITHUB,
            "kind": Credential.Kind.TOKEN,
            "secret": "private-token",
            "passphrase": "",
        }

    def make_credential(self):
        credential = Credential(name="Saved access", kind=Credential.Kind.TOKEN)
        credential.set_secret("saved-private-token")
        credential.save()
        return credential

    def test_credentials_are_listed_without_login(self):
        self.make_credential()
        response = self.client.get(self.url)
        self.assertContains(response, "Saved access")
        self.assertContains(response, "Save credential")

    def test_creation_accepts_valid_csrf_without_login(self):
        client = Client(enforce_csrf_checks=True)
        client.get(self.url)
        token = client.cookies["csrftoken"].value
        response = client.post(self.url, self.data, HTTP_X_CSRFTOKEN=token)
        self.assertRedirects(response, self.url)
        self.assertEqual(
            Credential.objects.get().get_secret()["secret"], "private-token"
        )

    def test_empty_page_explains_creation(self):
        response = self.client.get(self.url)
        self.assertContains(response, "No credentials yet.")
        self.assertContains(response, "Save credential")
        self.assertContains(response, "0 credentials")
        self.assertFalse(response.context["form"].fields["secret"].required)

    def test_form_has_name_provider_and_type_in_order(self):
        response = self.client.get(self.url)
        form = response.context["form"]
        self.assertEqual(
            list(form.fields),
            [
                "name",
                "provider",
                "kind",
                "secret",
                "passphrase",
                "connection_provider",
                "connection_endpoint",
                "connection_api_key",
            ],
        )
        self.assertEqual(
            list(form.fields["provider"].choices),
            list(Credential.Provider.choices),
        )
        self.assertEqual(
            form.fields["kind"].choices[1:],
            list(Credential.Kind.choices),
        )

    def test_gitlab_token_creation_shows_provider_and_type(self):
        self.data["provider"] = "gitlab"
        response = self.client.post(self.url, self.data, follow=True)
        self.assertRedirects(response, self.url)
        self.assertEqual(Credential.objects.get().provider, "gitlab")
        self.assertContains(response, "Provider: GitLab")
        self.assertContains(response, '<span class="priority">Token</span>', html=True)

    def test_other_secret_creation_encrypts_value(self):
        value = " custom secret\nwith spaces "
        self.data.update(provider="gitlab", kind="secret", secret=value)
        response = self.client.post(self.url, self.data, follow=True)
        self.assertRedirects(response, self.url)
        self.assertEqual(Credential.objects.get().get_secret()["secret"], value)
        self.assertNotContains(response, value)
        self.assertContains(
            response, '<span class="priority">Secret (other)</span>', html=True
        )

    def test_missing_provider_is_rejected(self):
        del self.data["provider"]
        response = self.client.post(self.url, self.data)
        self.assertIn("provider", response.context["form"].errors)
        self.assertFalse(Credential.objects.exists())

    def test_invalid_provider_is_rejected(self):
        self.data["provider"] = "unsupported"
        response = self.client.post(self.url, self.data)
        self.assertIn("provider", response.context["form"].errors)
        self.assertFalse(Credential.objects.exists())

    def test_successful_creation_lists_metadata_without_secret(self):
        response = self.client.post(self.url, self.data, follow=True)
        self.assertRedirects(response, self.url)
        self.assertContains(response, "Credential saved.")
        self.assertContains(response, "GitHub work account")
        self.assertContains(response, "1 credential")
        credential = Credential.objects.get()
        self.assertNotContains(response, "private-token")
        self.assertNotContains(response, credential.encrypted_payload)

    def test_ssh_key_creation_encrypts_private_key_and_passphrase(self):
        key = private_key("ssh-password")
        self.data.update(
            kind=Credential.Kind.SSH, secret=key, passphrase="ssh-password"
        )
        response = self.client.post(self.url, self.data, follow=True)
        self.assertRedirects(response, self.url)
        self.assertEqual(
            Credential.objects.get().get_secret(),
            {"secret": key, "passphrase": "ssh-password"},
        )
        self.assertNotContains(response, "ssh-password")
        self.assertNotContains(response, key)

    def test_invalid_form_keeps_name_but_never_echoes_secrets(self):
        self.data.update(kind=Credential.Kind.SSH, passphrase="private-passphrase")
        response = self.client.post(self.url, self.data)
        self.assertContains(response, 'value="GitHub work account"')
        self.assertContains(response, "Re-enter the secret before saving.")
        self.assertNotContains(response, "private-token")
        self.assertNotContains(response, "private-passphrase")
        self.assertFalse(Credential.objects.exists())

    def test_duplicate_name_shows_error_without_creating_credential(self):
        self.make_credential()
        self.data["name"] = "Saved access"
        response = self.client.post(self.url, self.data)
        self.assertIn("name", response.context["form"].errors)
        self.assertEqual(Credential.objects.count(), 1)

    def test_missing_secret_is_rejected(self):
        self.data["secret"] = ""
        response = self.client.post(self.url, self.data)
        self.assertContains(response, "Enter a secret value.")
        self.assertFalse(Credential.objects.exists())

    @override_settings(NQ_CREDENTIAL_KEY="")
    def test_missing_encryption_key_shows_error_without_saving(self):
        response = self.client.post(self.url, self.data)
        self.assertContains(response, "Configure VIBEARR_CREDENTIAL_KEY")
        self.assertNotContains(response, "private-token")
        self.assertFalse(Credential.objects.exists())

    def test_csrf_is_required(self):
        client = Client(enforce_csrf_checks=True)
        response = client.post(self.url, self.data)
        self.assertEqual(response.status_code, 403)
        self.assertFalse(Credential.objects.exists())

    def test_responses_cannot_be_cached(self):
        response = self.client.get(self.url)
        self.assertIn("no-store", response["Cache-Control"])
        self.assertIn("private", response["Cache-Control"])

    def test_unsupported_method_is_rejected(self):
        response = self.client.delete(self.url)
        self.assertEqual(response.status_code, 405)

    def test_credential_names_are_escaped(self):
        credential = self.make_credential()
        credential.name = "<script>alert('credential')</script>"
        credential.save()
        response = self.client.get(self.url)
        self.assertContains(response, "&lt;script&gt;")
        self.assertNotContains(response, credential.name)

    def test_navigation_links_to_credentials(self):
        response = self.client.get(reverse("index"))
        self.assertContains(response, f'href="{self.url}"')

    def test_credentials_navigation_marks_current_page(self):
        response = self.client.get(self.url)
        self.assertContains(
            response,
            f'<a href="{self.url}" aria-current="page">Credentials</a>',
            html=True,
        )

    def test_get_does_not_create_credentials(self):
        self.client.get(self.url, self.data)
        self.assertFalse(Credential.objects.exists())
