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
            "auto_sync_interval_seconds",
        ]
        labels = {
            "github_credential": "GitHub API credential",
            "auto_sync_interval_seconds": "Auto sync interval",
        }
        help_texts = {
            "github_credential": "Used for scheduled issue imports from autonomous GitHub projects.",
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
    connection_endpoint = forms.CharField(
        required=False,
        label="Ollama endpoint",
        help_text="Your external Ollama HTTP(S) origin and port. Vibearr does not provide Ollama or download models.",
    )

    class Meta:
        model = Credential
        fields = ["name", "provider", "kind", "secret"]
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
        secret.help_text = "For repository access, paste a GitHub token. For runner subscription login, leave blank and sign in inside the runner after saving. OpenCode uses the external Ollama fields."

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
        if {"connection_provider", "connection_api_key"}.intersection(self.data):
            self.add_error(
                None, "Use only the external Ollama endpoint or connection JSON."
            )
            return data
        endpoint = data.get("connection_endpoint")
        if endpoint:
            if data.get("provider") != "opencode" or data.get("kind") != "connection":
                self.add_error(
                    "connection_endpoint",
                    "Select OpenCode and connection type to use the Ollama endpoint.",
                )
                return data
            if data.get("secret"):
                self.add_error(
                    "connection_endpoint",
                    "Use either the Ollama endpoint or connection JSON.",
                )
                return data
            from vibearr_executor.ollama_relay import external_endpoint

            try:
                endpoint = external_endpoint(endpoint)
            except ValueError as exc:
                self.add_error("connection_endpoint", str(exc))
                return data
            data["secret"] = json.dumps({"provider": "ollama", "base_url": endpoint})
        secret = data.get("secret", "")
        if not secret:
            if not self.instance.pk:
                self.add_error("secret", "Enter a secret value.")
            return data
        kind = data.get("kind", self.instance.kind)
        if not kind:
            return data
        self.instance.kind = kind
        self.instance.provider = data.get("provider", self.instance.provider)
        try:
            self.instance.set_secret(secret)
        except ValidationError as exc:
            self.add_error(None, exc)
        except ImproperlyConfigured as exc:
            self.add_error(None, str(exc))
        return data
