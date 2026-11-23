from django import forms
from django.core.exceptions import ValidationError

from credentials.models import Credential

from .settings_models import ProjectRunnerSettings, RunnerSettings, RunnerSchedule
from .validators import BACKENDS, OVERRIDE_FIELDS


class ConfigurationJSONField(forms.JSONField):
    def prepare_value(self, value):
        prepared = super().prepare_value(value)
        return prepared.encode("utf-8", errors="backslashreplace").decode("utf-8")


class RunnerSettingsForm(forms.ModelForm):
    def clean_identity(self):
        from .models import Run

        identity = self.cleaned_data.get("identity")
        if self.instance.identity_id == getattr(identity, "pk", None):
            return identity
        affected = [self.instance.identity_id, getattr(identity, "pk", None)]
        if Run.objects.filter(runner_id__in=affected, state=Run.State.ACTIVE).exists():
            raise ValidationError(
                "Finish or cancel active work before changing runner assignments."
            )
        return identity

    def clean_merge_profile(self):
        value = self.cleaned_data.get("merge_profile")
        return {} if value is None else value

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["name"].required = True
        self.fields["identity"].help_text = (
            "Assign a provisioned runner identity to use this configuration. "
            "Unassigned identities use the default runner configuration."
        )
        self.fields["schedule"].help_text = (
            "Choose a reusable schedule. Changes on the Schedules page apply to every assigned runner."
        )
        for stage in ("research", "implementation"):
            self.fields[f"{stage}_model"].widget.attrs["list"] = "runner-claude-models"
            self.fields[f"{stage}_model"].help_text = (
                "Select the configured backend model. Native adapters must qualify before execution. Separate research settings are retained for history and do not gate native execution."
            )
            self.fields[f"{stage}_credential"].queryset = Credential.objects.filter(
                provider__in=BACKENDS
            ).defer("encrypted_payload")

    class Meta:
        model = RunnerSettings
        exclude = ["id"]
        labels = {"minimum_remaining_usage_percent": "Minimum remaining usage (%)"}
        field_classes = {
            field: ConfigurationJSONField
            for field in (
                "setup_commands",
                "validation_commands",
                "execution_policy",
                "merge_profile",
            )
        }
        help_texts = {
            "workspace_retention_days": "Remove only acknowledged terminal workspaces; active, waiting, and uncertain reporting remain protected.",
            "plan_retention_days": "Expire context and plan contents after this many days. Approval context and reconciliation evidence remain protected; history keeps tombstones.",
            "log_retention_days": "Expire protected local logs and uploaded artifacts for acknowledged terminal runs. Keep delivery, validation, stage, and recovery summaries.",
            "enabled": "Execution stays release-gated until live integration verification succeeds.",
            "paused": "Pause current work at the next safe checkpoint and prevent new claims.",
            "reserve_enabled": "Block all projects if any participating account lacks fresh allowance or is at/below the reserve.",
            "minimum_remaining_usage_percent": "40 permits work only while more than 40% remains. Zero still blocks exhausted accounts.",
            "setup_commands": "JSON list of setup commands, restricted to the configured isolated task policy.",
            "execution_policy": "Versioned native execution limits. Task content cannot change this policy. Runtime and agent installation are still required.",
            "merge_profile": "Trusted required workflows and optional review producers. Required for automatic merge; leave unset for PR-only delivery.",
            "validation_commands": "JSON list of required validation commands.",
        }
        widgets = {
            "setup_commands": forms.Textarea(attrs={"rows": 3}),
            "validation_commands": forms.Textarea(attrs={"rows": 3}),
            "research_instructions": forms.Textarea(attrs={"rows": 4}),
            "implementation_instructions": forms.Textarea(attrs={"rows": 4}),
        }


class RunnerScheduleForm(forms.ModelForm):
    class Meta:
        model = RunnerSchedule
        fields = ["name", "schedule_mode", "timezone", "windows"]
        labels = {"schedule_mode": "Processing mode"}
        field_classes = {"windows": ConfigurationJSONField}
        widgets = {"windows": forms.Textarea(attrs={"rows": 4})}
        help_texts = {
            "timezone": "IANA timezone, such as America/Los_Angeles. Daylight saving changes follow this timezone.",
            "windows": 'JSON list: [{"days": [0,1,2,3,4], "start": "22:00", "end": "06:00"}]. Monday is 0; overnight windows belong to their starting day.',
        }


class ProjectRunnerSettingsForm(forms.Form):
    def __init__(self, *args, instance=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.instance = instance
        saved = instance.overrides if instance else {}
        for key in OVERRIDE_FIELDS:
            label = key.replace("_id", "").replace("_", " ").title()
            choices = [
                ("inherit", "Inherit runner settings"),
                ("replace", "Use project value"),
            ]
            if key != "factory":
                choices.append(("clear", "Clear value"))
            mode = forms.ChoiceField(
                label=f"{label}: source",
                choices=choices,
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
            elif key.endswith("commands") or key in {
                "execution_policy",
                "merge_profile",
                "factory",
            }:
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
            if key.endswith("model"):
                value.help_text = "Use the configured backend model. Native adapters must qualify before execution."
                value.widget.attrs["list"] = "runner-claude-models"
            if key == "factory":
                value.help_text = (
                    "To disable factory execution for this project, choose Use project value "
                    'and enter {"enabled": false}.'
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
                message = "Enter a project value or choose Clear value."
                if key == "factory":
                    message = (
                        "Enter factory JSON or choose Inherit runner settings. "
                        'To disable factory execution, use {"enabled": false}.'
                    )
                self.add_error(key, message)
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
