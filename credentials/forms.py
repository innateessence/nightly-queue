from django import forms
from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.views.decorators.debug import sensitive_variables

from .models import APISettings, Credential


class APISettingsForm(forms.ModelForm):
    class Meta:
        model = APISettings
        fields = ["github_credential", "gitlab_credential"]
        labels = {
            "github_credential": "GitHub API credential",
            "gitlab_credential": "GitLab API credential",
        }
        help_texts = {
            "github_credential": "Used for hourly issue imports from autonomous GitHub projects.",
            "gitlab_credential": "Saved for GitLab API access. GitLab issue imports are not available yet.",
        }


class SecretTextarea(forms.Textarea):
    def get_context(self, name, value, attrs):
        return super().get_context(name, None, attrs)


class CredentialForm(forms.ModelForm):
    secret = forms.CharField(
        required=False,
        strip=False,
        widget=SecretTextarea(
            attrs={"autocomplete": "off", "spellcheck": "false", "rows": 5}
        ),
        help_text="Paste the secret value. Leave blank when editing to keep the saved secret.",
    )
    passphrase = forms.CharField(
        required=False,
        strip=False,
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
        help_text="For an encrypted SSH key. Supply it again when replacing the key.",
    )

    class Meta:
        model = Credential
        fields = ["name", "provider", "kind", "secret", "passphrase"]
        labels = {"kind": "Type"}
        widgets = {
            "name": forms.TextInput(
                attrs={"placeholder": "e.g. Work repository access"}
            )
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name, field in self.fields.items():
            field.widget.attrs["aria-describedby"] = (
                f"id_{name}_helptext" if field.help_text else ""
            )
        if self.instance.pk:
            return
        self.fields["kind"].initial = Credential.Kind.TOKEN
        secret = self.fields["secret"]
        secret.required = True
        secret.error_messages["required"] = "Enter a secret value."
        secret.help_text = "Paste a token, a complete SSH private key (OpenSSH or PEM), or another secret."
        self.fields["passphrase"].help_text = (
            "Only needed for a passphrase-protected SSH key."
        )

    @sensitive_variables()
    def clean(self):
        data = super().clean()
        secret = data.get("secret", "")
        passphrase = data.get("passphrase", "")
        if not secret:
            if not self.instance.pk:
                self.add_error("secret", "Enter a secret value.")
            elif passphrase:
                self.add_error(
                    "passphrase",
                    "Supply the private key again to change its passphrase.",
                )
            return data
        kind = data.get("kind", self.instance.kind)
        if not kind:
            return data
        self.instance.kind = kind
        try:
            self.instance.set_secret(secret, passphrase)
        except ValidationError as exc:
            self.add_error(None, exc)
        except ImproperlyConfigured as exc:
            self.add_error(None, str(exc))
        return data
