from django import forms
from django.core.exceptions import ValidationError

from credentials.models import Credential

from .settings_models import ProjectRunnerSettings, RunnerSettings
from .validators import BACKENDS, OVERRIDE_FIELDS


class ConfigurationJSONField(forms.JSONField):
    def prepare_value(self, value):
        prepared = super().prepare_value(value)
        return prepared.encode("utf-8", errors="backslashreplace").decode("utf-8")


class RunnerSettingsForm(forms.ModelForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for stage in ("research", "implementation"):
            self.fields[f"{stage}_credential"].queryset = Credential.objects.filter(
                provider__in=BACKENDS
            ).defer("encrypted_payload")

    class Meta:
        model = RunnerSettings
        exclude = ["id"]
        labels = {"minimum_remaining_usage_percent": "Minimum remaining usage (%)"}
        field_classes = {
            field: ConfigurationJSONField
            for field in ("windows", "setup_commands", "validation_commands")
        }
        help_texts = {
            "enabled": "Execution stays release-gated until live integration verification succeeds.",
            "paused": "Pause current work at the next safe checkpoint and prevent new claims.",
            "windows": 'JSON list: [{"days": [0,1,2,3,4], "start": "22:00", "end": "06:00"}]. Days start on Monday; overnight windows belong to their starting day.',
            "timezone": "IANA timezone. Schedule evaluation and DST policy belong to the execution gates.",
            "reserve_enabled": "Block all projects if any participating account lacks fresh allowance or is at/below the reserve.",
            "minimum_remaining_usage_percent": "40 permits work only while more than 40% remains. Zero still blocks exhausted accounts.",
            "setup_commands": "JSON list of setup commands. Untrusted commands require separate approval.",
            "validation_commands": "JSON list of required validation commands.",
        }
        widgets = {
            "windows": forms.Textarea(attrs={"rows": 3}),
            "setup_commands": forms.Textarea(attrs={"rows": 3}),
            "validation_commands": forms.Textarea(attrs={"rows": 3}),
            "research_instructions": forms.Textarea(attrs={"rows": 4}),
            "implementation_instructions": forms.Textarea(attrs={"rows": 4}),
        }


class ProjectRunnerSettingsForm(forms.Form):
    def __init__(self, *args, instance=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.instance = instance
        saved = instance.overrides if instance else {}
        for key in OVERRIDE_FIELDS:
            label = key.replace("_id", "").replace("_", " ").title()
            mode = forms.ChoiceField(
                label=f"{label}: source",
                choices=[
                    ("inherit", "Inherit global"),
                    ("replace", "Use project value"),
                    ("clear", "Clear value"),
                ],
                required=False,
            )
            self.fields[f"{key}_mode"] = mode
            if key.endswith("credential_id"):
                value = forms.ModelChoiceField(
                    queryset=(
                        Credential.objects.filter(provider__in=BACKENDS).defer(
                            "encrypted_payload"
                        )
                        if key.startswith(("research", "implementation"))
                        else Credential.objects.filter(
                            provider="github", kind="token"
                        ).defer("encrypted_payload")
                    ),
                    required=False,
                    label=label,
                )
            elif key.endswith("backend"):
                value = forms.ChoiceField(
                    choices=[("", "Choose backend")]
                    + [(backend, backend.title()) for backend in BACKENDS],
                    required=False,
                    label=label,
                )
            elif key.endswith("commands"):
                value = ConfigurationJSONField(
                    required=False,
                    label=label,
                    widget=forms.Textarea(attrs={"rows": 3}),
                )
            else:
                value = forms.CharField(
                    required=False,
                    label=label,
                    max_length=20000 if key.endswith("instructions") else 200,
                    widget=(
                        forms.Textarea(attrs={"rows": 3})
                        if key.endswith("instructions")
                        else forms.TextInput()
                    ),
                )
            self.fields[key] = value
            entry = saved.get(key, {})
            self.initial[f"{key}_mode"] = entry.get("mode", "inherit")
            self.initial[key] = entry.get("value")

    def clean(self):
        data = super().clean()
        overrides = {}
        for key in OVERRIDE_FIELDS:
            mode = data.get(f"{key}_mode") or "inherit"
            value = data.get(key)
            if mode == "replace" and value in (None, ""):
                self.add_error(key, "Enter a project value or choose Clear value.")
                continue
            if key.endswith("credential_id") and value:
                value = value.pk
            overrides[key] = {"mode": mode}
            if mode == "replace":
                overrides[key]["value"] = value
        instance = ProjectRunnerSettings(overrides=overrides)
        try:
            instance.clean_fields(exclude=["project"])
        except ValidationError as exc:
            self.add_error(None, "; ".join(exc.messages))
        self.overrides = overrides
        return data

    def save(self, project):
        instance, _ = ProjectRunnerSettings.objects.get_or_create(project=project)
        instance.overrides = self.overrides
        instance.save()
        return instance
