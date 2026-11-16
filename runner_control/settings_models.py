from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models, transaction

from .validators import (
    BACKENDS,
    validate_commands,
    validate_backend_model,
    validate_overrides,
    validate_timezone,
    validate_windows,
)


class RunnerSettings(models.Model):
    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    enabled = models.BooleanField(default=False)
    paused = models.BooleanField(default=False)
    schedule_mode = models.CharField(
        max_length=12,
        choices=[("continuous", "Continuous"), ("windows", "Weekday windows")],
        default="continuous",
    )
    timezone = models.CharField(
        max_length=100, default="UTC", validators=[validate_timezone]
    )
    windows = models.JSONField(default=list, blank=True, validators=[validate_windows])
    polling_interval_seconds = models.PositiveIntegerField(
        default=60, validators=[MinValueValidator(1), MaxValueValidator(86400)]
    )
    tasks_per_window = models.PositiveIntegerField(
        default=1, validators=[MinValueValidator(1)]
    )
    task_timeout_seconds = models.PositiveIntegerField(
        default=7200, validators=[MinValueValidator(1)]
    )
    research_timeout_seconds = models.PositiveIntegerField(
        default=1800, validators=[MinValueValidator(1)]
    )
    implementation_timeout_seconds = models.PositiveIntegerField(
        default=3600, validators=[MinValueValidator(1)]
    )
    validation_timeout_seconds = models.PositiveIntegerField(
        default=1800, validators=[MinValueValidator(1)]
    )
    reserve_enabled = models.BooleanField(default=False)
    minimum_remaining_usage_percent = models.PositiveSmallIntegerField(
        default=40, validators=[MaxValueValidator(100)]
    )
    research_backend = models.CharField(
        max_length=30,
        choices=[(backend, backend.title()) for backend in BACKENDS],
        blank=True,
    )
    research_model = models.CharField(max_length=200, blank=True)
    research_instructions = models.TextField(blank=True, max_length=20000)
    research_credential = models.ForeignKey(
        "credentials.Credential",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="research_runner_settings",
    )
    implementation_backend = models.CharField(
        max_length=30,
        choices=[(backend, backend.title()) for backend in BACKENDS],
        blank=True,
    )
    implementation_model = models.CharField(max_length=200, blank=True)
    implementation_instructions = models.TextField(blank=True, max_length=20000)
    implementation_credential = models.ForeignKey(
        "credentials.Credential",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="implementation_runner_settings",
    )
    issue_credential = models.ForeignKey(
        "credentials.Credential",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="issue_runner_settings",
        limit_choices_to={"provider": "github", "kind": "token"},
    )
    repository_credential = models.ForeignKey(
        "credentials.Credential",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="repository_runner_settings",
        limit_choices_to={"provider": "github", "kind": "token"},
    )
    setup_commands = models.JSONField(
        default=list, blank=True, validators=[validate_commands]
    )
    validation_commands = models.JSONField(
        default=list, blank=True, validators=[validate_commands]
    )
    workspace_retention_days = models.PositiveIntegerField(
        default=14, validators=[MinValueValidator(1)]
    )
    plan_retention_days = models.PositiveIntegerField(
        default=90, validators=[MinValueValidator(1)]
    )
    log_retention_days = models.PositiveIntegerField(
        default=30, validators=[MinValueValidator(1)]
    )

    def clean(self):
        super().clean()
        for field, validator in (
            ("windows", validate_windows),
            ("setup_commands", validate_commands),
            ("validation_commands", validate_commands),
        ):
            value = getattr(self, field)
            # Django skips field validators for empty JSON values.
            if value not in self._meta.get_field(field).empty_values:
                continue
            try:
                validator(value)
            except ValidationError as exc:
                raise ValidationError({field: exc.messages}) from None
        if self.pk != 1:
            raise ValidationError("Only one runner settings record is allowed.")
        if self.schedule_mode == "windows" and not self.windows:
            raise ValidationError({"windows": "Add at least one processing window."})
        for stage in ("research", "implementation"):
            try:
                validate_backend_model(
                    getattr(self, f"{stage}_backend"), getattr(self, f"{stage}_model")
                )
            except ValidationError as exc:
                raise ValidationError({f"{stage}_model": exc.messages}) from None
        for field in (
            "research_timeout_seconds",
            "implementation_timeout_seconds",
            "validation_timeout_seconds",
        ):
            if getattr(self, field) > self.task_timeout_seconds:
                raise ValidationError(
                    {field: "Stage timeout cannot exceed the task timeout."}
                )
        for field in ("issue_credential", "repository_credential"):
            credential = getattr(self, field)
            if credential and (
                credential.provider != "github" or credential.kind != "token"
            ):
                raise ValidationError({field: "Select a GitHub token credential."})

    def save(self, *args, **kwargs):
        self.full_clean()
        with transaction.atomic():
            list(RunnerSettings.objects.select_for_update().filter(pk=1))
            return super().save(*args, **kwargs)


class ProjectRunnerSettings(models.Model):
    project = models.OneToOneField(
        "projects.Project", on_delete=models.CASCADE, related_name="runner_settings"
    )
    overrides = models.JSONField(
        default=dict, blank=True, validators=[validate_overrides]
    )

    def clean(self):
        super().clean()
        if self.overrides not in self._meta.get_field("overrides").empty_values:
            return
        try:
            validate_overrides(self.overrides)
        except ValidationError as exc:
            raise ValidationError({"overrides": exc.messages}) from None

    def save(self, *args, **kwargs):
        self.full_clean()
        with transaction.atomic():
            list(RunnerSettings.objects.select_for_update().filter(pk=1))
            return super().save(*args, **kwargs)
