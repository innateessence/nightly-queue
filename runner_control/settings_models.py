from django.core.exceptions import ValidationError
from django.core.management.color import no_style
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import connection, models, transaction

from vibearr_runner.policy import default_execution_policy

from .validators import (
    BACKENDS,
    validate_commands,
    validate_backend_model,
    validate_overrides,
    validate_timezone,
    validate_windows,
    validate_execution_policy,
)


class RunnerSchedule(models.Model):
    name = models.CharField(max_length=100, unique=True)
    schedule_mode = models.CharField(
        max_length=12,
        choices=[("continuous", "Continuous"), ("windows", "Weekday windows")],
        default="continuous",
    )
    timezone = models.CharField(
        max_length=100, default="UTC", validators=[validate_timezone]
    )
    windows = models.JSONField(default=list, blank=True, validators=[validate_windows])

    class Meta:
        ordering = ["name", "pk"]

    def clean(self):
        super().clean()
        try:
            validate_windows(self.windows)
        except ValidationError as exc:
            raise ValidationError({"windows": exc.messages}) from None
        if self.schedule_mode == "windows" and not self.windows:
            raise ValidationError({"windows": "Add at least one processing window."})

    def save(self, *args, **kwargs):
        with transaction.atomic():
            list(
                RunnerSettings.objects.select_for_update()
                .filter(pk=1)
                .values_list("pk", flat=True)
            )
            if (
                not self._state.adding
                and not RunnerSchedule.objects.filter(pk=self.pk).exists()
            ):
                raise ValidationError("This schedule was deleted.")
            if self.pk is None:
                ensure_default_schedule()
            self.full_clean()
            return super().save(*args, **kwargs)

    def __str__(self):
        return self.name


def ensure_default_schedule():
    with transaction.atomic():
        saved, created = RunnerSchedule.objects.get_or_create(
            pk=1, defaults={"name": "Always on"}
        )
        if created:
            with connection.cursor() as cursor:
                for statement in connection.ops.sequence_reset_sql(
                    no_style(), [RunnerSchedule]
                ):
                    cursor.execute(statement)
        return saved


class RunnerSettings(models.Model):
    id = models.BigAutoField(primary_key=True)
    name = models.CharField(max_length=100, default="Default runner")
    identity = models.OneToOneField(
        "runner_control.RunnerIdentity",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="configuration",
    )
    enabled = models.BooleanField(default=False)
    paused = models.BooleanField(default=False)
    schedule = models.ForeignKey(
        RunnerSchedule, on_delete=models.PROTECT, default=1, related_name="runners"
    )
    execution_policy = models.JSONField(
        default=default_execution_policy, validators=[validate_execution_policy]
    )
    polling_interval_seconds = models.PositiveIntegerField(
        default=60, validators=[MinValueValidator(1), MaxValueValidator(86400)]
    )
    tasks_per_window = models.PositiveIntegerField(
        default=1, validators=[MinValueValidator(1)]
    )
    task_timeout_seconds = models.PositiveIntegerField(
        default=7200, validators=[MinValueValidator(1)]
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
        from credentials.models import Credential

        for field in (
            "implementation_credential",
            "issue_credential",
            "repository_credential",
        ):
            reference = getattr(self, f"{field}_id")
            if reference and not Credential.objects.filter(pk=reference).exists():
                raise ValidationError({field: "Select an available credential."})
        for field, validator in (
            ("execution_policy", validate_execution_policy),
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
        for stage in ("implementation",):
            try:
                validate_backend_model(
                    getattr(self, f"{stage}_backend"), getattr(self, f"{stage}_model")
                )
            except ValidationError as exc:
                raise ValidationError({f"{stage}_model": exc.messages}) from None
        for field in (
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
        with transaction.atomic():
            list(RunnerSettings.objects.select_for_update().filter(pk=1))
            if (
                not self._state.adding
                and not RunnerSettings.objects.filter(pk=self.pk).exists()
            ):
                raise ValidationError("This coding runner was deleted.")
            if self.schedule_id == 1:
                ensure_default_schedule()
            self.full_clean()
            return super().save(*args, **kwargs)

    def __str__(self):
        return self.name or "Default runner"


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
            from projects.models import Project

            if not Project.objects.filter(pk=self.project_id).exists():
                raise ValidationError("This project was deleted.")
            return super().save(*args, **kwargs)
