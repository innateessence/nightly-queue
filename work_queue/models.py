from django.db import models

from projects.models import Project


class QueueItem(models.Model):
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
        constraints = [
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

    def __str__(self):
        return self.title


class RunnerLog(models.Model):
    item = models.ForeignKey(
        QueueItem, on_delete=models.CASCADE, related_name="runner_logs"
    )
    run_id = models.UUIDField()
    sequence = models.PositiveSmallIntegerField()
    text = models.TextField(max_length=4096)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["sequence"]
        constraints = [
            models.UniqueConstraint(
                fields=["item", "run_id", "sequence"], name="unique_runner_log_sequence"
            ),
        ]
