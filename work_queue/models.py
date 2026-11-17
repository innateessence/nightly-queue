from django.core.exceptions import ValidationError
from django.db import models, transaction

from projects.models import Project
from vibearr_app.managers import ActiveManager


class QueueItem(models.Model):
    objects = ActiveManager()
    all_objects = models.Manager()
    deleted_at = models.DateTimeField(
        null=True, blank=True, editable=False, db_index=True
    )

    class Source(models.TextChoices):
        MANUAL = "manual", "Manual"
        GITHUB = "github", "GitHub issue"

    class IssueState(models.TextChoices):
        OPEN = "open", "Open"
        CLOSED = "closed", "Closed"

    class Priority(models.TextChoices):
        LOW = "low", "Low"
        NORMAL = "normal", "Normal"
        HIGH = "high", "High"

    class Status(models.TextChoices):
        QUEUED = "queued", "Queued"
        RUNNING = "running", "Running"
        SUCCEEDED = "succeeded", "Succeeded"
        FAILED = "failed", "Failed"

    project = models.ForeignKey(Project, on_delete=models.PROTECT, related_name="items")
    source = models.CharField(
        max_length=6, choices=Source.choices, default=Source.MANUAL
    )
    issue_number = models.PositiveBigIntegerField(null=True, blank=True, editable=False)
    issue_repository = models.URLField(max_length=500, blank=True, editable=False)
    issue_state = models.CharField(
        max_length=6, choices=IssueState.choices, blank=True, editable=False
    )
    github_repository_id = models.PositiveBigIntegerField(
        null=True, blank=True, editable=False
    )
    github_issue_id = models.PositiveBigIntegerField(
        null=True, blank=True, editable=False
    )
    author_id = models.PositiveBigIntegerField(null=True, blank=True, editable=False)
    author_login = models.CharField(max_length=100, blank=True, editable=False)
    issue_labels = models.JSONField(default=list, blank=True, editable=False)
    issue_created_at = models.DateTimeField(null=True, blank=True, editable=False)
    issue_updated_at = models.DateTimeField(null=True, blank=True, editable=False)
    issue_revision = models.CharField(max_length=64, blank=True, editable=False)
    issue_synced_at = models.DateTimeField(null=True, blank=True, editable=False)
    source_metadata = models.JSONField(default=dict, blank=True, editable=False)
    source_snapshot = models.JSONField(default=dict, blank=True, editable=False)
    requested_branch = models.CharField(max_length=200, blank=True, editable=False)
    dependencies = models.JSONField(default=list, blank=True, editable=False)
    selection_error = models.CharField(max_length=300, blank=True, editable=False)
    title = models.CharField(max_length=200)
    instructions = models.TextField(blank=True)
    priority = models.CharField(
        max_length=6, choices=Priority.choices, default=Priority.NORMAL
    )
    created_at = models.DateTimeField(auto_now_add=True)
    status = models.CharField(
        max_length=9, choices=Status.choices, default=Status.QUEUED
    )
    claim_token = models.UUIDField(null=True, blank=True, editable=False)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    branch_name = models.CharField(max_length=200, blank=True)
    execution_ref = models.CharField(max_length=200, blank=True)
    result_url = models.URLField(max_length=500, blank=True)
    error_summary = models.TextField(blank=True)
    # Retries keep the first claim's checkout settings.
    repository_url = models.URLField(max_length=500, blank=True, editable=False)
    base_branch = models.CharField(max_length=200, blank=True, editable=False)

    class Meta:
        ordering = ["created_at", "pk"]
        base_manager_name = "all_objects"
        default_manager_name = "objects"
        constraints = [
            models.UniqueConstraint(
                fields=["project", "github_repository_id", "issue_number"],
                name="unique_project_github_issue",
            ),
            models.UniqueConstraint(
                fields=["project", "branch_name"],
                condition=~models.Q(branch_name=""),
                name="unique_project_work_branch",
            ),
            models.UniqueConstraint(
                fields=["project", "issue_repository", "issue_number"],
                name="unique_project_issue",
            ),
        ]
        indexes = [
            models.Index(
                fields=["status", "created_at", "id"], name="queue_claim_order"
            )
        ]

    def save(self, *args, **kwargs):
        with transaction.atomic():
            project = (
                Project.all_objects.select_for_update()
                .filter(pk=self.project_id)
                .first()
            )
            if project and project.deleted_at:
                raise ValidationError("This project was deleted.")
            if (
                self.pk
                and QueueItem.all_objects.filter(
                    pk=self.pk, deleted_at__isnull=False
                ).exists()
            ):
                raise ValidationError("This work item was deleted.")
            return super().save(*args, **kwargs)

    def __str__(self):
        return self.title


class MergeEvidence(models.Model):
    repository_id = models.PositiveBigIntegerField()
    issue_number = models.PositiveBigIntegerField()
    pull_number = models.PositiveBigIntegerField()
    merge_sha = models.CharField(max_length=64)
    merged_at = models.DateTimeField()
    recorded_at = models.DateTimeField(auto_now_add=True)
    provenance = models.CharField(max_length=100)
    operation_id = models.CharField(max_length=200, unique=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["repository_id", "issue_number"],
                name="unique_issue_merge_evidence",
            )
        ]
