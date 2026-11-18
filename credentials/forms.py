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
            ("ollama", "External Ollama"),
        ],
        help_text="OpenCode runs inside the runner and uses your external Ollama server.",
    )
    connection_endpoint = forms.CharField(
        required=False,
        label="Ollama endpoint",
        help_text="Your external Ollama HTTP(S) origin and port. Vibearr does not provide Ollama or download models.",
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
        secret.help_text = "For repository access, paste a token or SSH key. For runner subscription login, leave blank and sign in inside the runner after saving. OpenCode uses the external Ollama fields."
        self.fields["passphrase"].help_text = (
            "Only needed for a passphrase-protected SSH key."
        )

    @sensitive_variables()
    def clean(self):
        data = super().clean()
        if data.get("kind") == "login":
            if data.get("provider") not in {"codex", "claude"}:
                self.add_error(
                    "kind", "Subscription login is available for Codex and Claude Code."
                )
                return data
            if data.get("secret"):
                self.add_error(
                    "secret",
                    "Leave the secret blank and sign in with your subscription inside the runner.",
                )
                return data
            data["secret"] = "" if self.instance.pk else "managed-subscription-login"
        connection_provider = data.get("connection_provider")
        connection_fields = (
            connection_provider
            or data.get("connection_endpoint")
            or self.data.get("connection_api_key")
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
            if self.data.get("connection_api_key"):
                value["api_key"] = self.data["connection_api_key"]
            from vibearr_executor.ollama_relay import external_endpoint

            try:
                if connection_provider != "ollama" or value.get("api_key"):
                    raise ValueError(
                        "OpenCode requires external Ollama without a model API key."
                    )
                value["base_url"] = external_endpoint(value.get("base_url"))
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
