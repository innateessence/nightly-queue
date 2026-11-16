import json

from django import forms
from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.views.decorators.debug import sensitive_variables

from .models import APISettings, Credential


class APISettingsForm(forms.ModelForm):
    auto_sync_interval_seconds = forms.IntegerField(
        min_value=60,
        max_value=86400,
        label="Auto sync interval (seconds)",
        help_text="Default 60 seconds. Sync issues now runs immediately.",
    )

    class Meta:
        model = APISettings
        fields = [
            "github_credential",
            "gitlab_credential",
            "auto_sync_interval_seconds",
        ]
        labels = {
            "github_credential": "GitHub API credential",
            "gitlab_credential": "GitLab API credential",
            "auto_sync_interval_seconds": "Auto sync interval",
        }
        help_texts = {
            "github_credential": "Used for scheduled issue imports from autonomous GitHub projects.",
            "gitlab_credential": "Saved for GitLab API access. GitLab issue imports are not available yet.",
            "auto_sync_interval_seconds": "How often to sync issues for all autonomous projects. Sync issues now runs immediately.",
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

    connection_provider = forms.ChoiceField(
        required=False,
        label="OpenCode inference provider",
        choices=[
            ("", "Use connection JSON or keep saved connection"),
            ("ollama", "Trusted local Ollama"),
            ("openai", "OpenAI API"),
            ("anthropic", "Anthropic API"),
            ("opencode", "OpenCode Console Chat API"),
        ],
        help_text="For OpenCode connection credentials. Native OpenCode CLI execution is unsupported; this uses a tool-free provider API candidate.",
    )
    connection_endpoint = forms.CharField(
        required=False,
        label="Ollama endpoint",
        help_text="For local Ollama: http://runner-ollama:11434 or an explicitly trusted local IP/loopback host. No URL credentials, query or fragment. Hosted providers use fixed official endpoints.",
    )
    connection_api_key = forms.CharField(
        required=False,
        strip=False,
        label="Inference API key",
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
        help_text="Required for hosted providers; optional for local Ollama. To rotate, supply the provider and complete replacement connection. Fields are never read back.",
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
        secret.required = False
        secret.error_messages["required"] = "Enter a secret value."
        secret.help_text = "Paste a token, SSH key or Codex session JSON. For OpenCode, use the inference fields below or paste connection JSON."
        self.fields["passphrase"].help_text = (
            "Only needed for a passphrase-protected SSH key."
        )

    @sensitive_variables()
    def clean(self):
        data = super().clean()
        connection_provider = data.get("connection_provider")
        connection_fields = (
            connection_provider
            or data.get("connection_endpoint")
            or data.get("connection_api_key")
        )
        if connection_fields:
            if data.get("provider") != "opencode" or data.get("kind") != "connection":
                self.add_error(
                    "connection_provider",
                    "Select OpenCode and connection type to use inference fields.",
                )
                return data
            if not connection_provider or data.get("secret"):
                self.add_error(
                    "connection_provider",
                    "Choose a provider and use either inference fields or connection JSON.",
                )
                return data
            value = {"provider": connection_provider}
            if data.get("connection_endpoint"):
                value["base_url"] = data["connection_endpoint"]
            if data.get("connection_api_key"):
                value["api_key"] = data["connection_api_key"]
            from vibearr_runner.opencode_connection import validate_connection

            try:
                validate_connection(value)
            except ValueError as exc:
                self.add_error("connection_provider", str(exc))
                return data
            data["secret"] = json.dumps(value)
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
        self.instance.provider = data.get("provider", self.instance.provider)
        try:
            self.instance.set_secret(secret, passphrase)
        except ValidationError as exc:
            self.add_error(None, exc)
        except ImproperlyConfigured as exc:
            self.add_error(None, str(exc))
        return data
