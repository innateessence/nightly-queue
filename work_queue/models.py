from django.db import models

from projects.models import Project


class QueueItem(models.Model):
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
        indexes = [
            models.Index(
                fields=["status", "created_at", "id"], name="queue_claim_order"
            )
        ]

    def __str__(self):
        return self.title
