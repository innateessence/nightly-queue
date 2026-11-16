from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.utils import timezone

from .validators import normalize_repository_url, validate_branch


class Project(models.Model):
    name = models.CharField(max_length=200)
    repository_url = models.URLField(max_length=500)
    base_branch = models.CharField(
        max_length=200, blank=True, validators=[validate_branch]
    )
    autonomous = models.BooleanField(default=False)
    github_repository_id = models.PositiveBigIntegerField(
        null=True, blank=True, editable=False
    )
    github_owner_id = models.PositiveBigIntegerField(
        null=True, blank=True, editable=False
    )
    github_default_branch = models.CharField(max_length=200, blank=True, editable=False)
    permitted_author_ids = models.JSONField(default=list, blank=True)
    enabled_at = models.DateTimeField(null=True, blank=True, editable=False)
    backfill_issues = models.BooleanField(default=False)
    sync_attempt_at = models.DateTimeField(null=True, blank=True, editable=False)
    sync_success_at = models.DateTimeField(null=True, blank=True, editable=False)
    sync_not_before = models.DateTimeField(null=True, blank=True, editable=False)
    sync_lock_token = models.UUIDField(null=True, blank=True, editable=False)
    sync_lock_until = models.DateTimeField(null=True, blank=True, editable=False)
    sync_etags = models.JSONField(default=dict, blank=True, editable=False)
    sync_error = models.CharField(max_length=300, blank=True, editable=False)
    credential = models.ForeignKey(
        "credentials.Credential", on_delete=models.PROTECT, null=True, blank=True
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name", "pk"]

    @classmethod
    def from_db(cls, db, field_names, values):
        instance = super().from_db(db, field_names, values)
        if "repository_url" in field_names:
            instance._loaded_repository_url = instance.repository_url
        return instance

    def refresh_from_db(self, using=None, fields=None):
        super().refresh_from_db(using=using, fields=fields)
        if fields is None or "repository_url" in fields:
            self._loaded_repository_url = self.repository_url

    def clean(self):
        super().clean()
        if not self.name or not self.name.strip():
            raise ValidationError({"name": "Enter a project name."})
        self.name = self.name.strip()
        self.repository_url = normalize_repository_url(self.repository_url)
        previous = Project.objects.filter(pk=self.pk).first()
        if (
            previous
            and previous.github_repository_id
            and previous.repository_url != self.repository_url
            and self.repository_url != getattr(self, "_loaded_repository_url", None)
        ):
            raise ValidationError(
                {
                    "repository_url": "A synchronized repository is identified by GitHub ID. Create another project to select a different repository."
                }
            )
        if not isinstance(self.permitted_author_ids, list) or any(
            type(value) is not int or not 0 < value < 2**63
            for value in self.permitted_author_ids
        ):
            raise ValidationError(
                {
                    "permitted_author_ids": "Enter a JSON list of positive GitHub user IDs."
                }
            )

    def save(self, *args, **kwargs):
        from runner_control.settings_models import RunnerSettings

        with transaction.atomic():
            RunnerSettings.objects.get_or_create(pk=1)
            RunnerSettings.objects.select_for_update().get(pk=1)
            previous = Project.objects.select_for_update().filter(pk=self.pk).first()
            if previous:
                unchanged_url = self.repository_url == getattr(
                    self, "_loaded_repository_url", None
                )
                if unchanged_url:
                    self.repository_url = previous.repository_url
                for field in self._meta.concrete_fields:
                    managed = field.name.startswith(
                        ("github_", "sync_")
                    ) or field.name in {"enabled_at", "created_at"}
                    if managed:
                        setattr(self, field.name, getattr(previous, field.name))
                if (
                    previous.github_repository_id
                    and previous.repository_url
                    != normalize_repository_url(self.repository_url)
                ):
                    raise ValidationError(
                        {
                            "repository_url": "A synchronized repository is identified by GitHub ID. Create another project to select a different repository."
                        }
                    )
            self.full_clean()
            if self.autonomous and not self.enabled_at:
                self.enabled_at = timezone.now()
                if kwargs.get("update_fields") is not None:
                    kwargs["update_fields"] = set(kwargs["update_fields"]) | {
                        "enabled_at"
                    }
            result = super().save(*args, **kwargs)
            self._loaded_repository_url = self.repository_url
            return result

    def __str__(self):
        return f"{self.name} (#{self.pk})"
