from django import forms
from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.views.decorators.debug import sensitive_variables

from .models import Credential


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
        help_text="Paste a token or complete private key. Leave blank when editing to keep the saved secret.",
    )
    passphrase = forms.CharField(
        required=False,
        strip=False,
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
        help_text="For an encrypted SSH key. Supply it again when replacing the key.",
    )

    class Meta:
        model = Credential
        fields = ["name", "kind", "secret", "passphrase"]

    @sensitive_variables()
    def clean(self):
        data = super().clean()
        secret = data.get("secret", "")
        passphrase = data.get("passphrase", "")
        if not secret:
            if not self.instance.pk:
                self.add_error("secret", "Enter a token or private key.")
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
