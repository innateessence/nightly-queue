import uuid

from django.db import models


class RunRetention(models.Model):
    run = models.OneToOneField(
        "runner_control.Run", on_delete=models.PROTECT, related_name="retention"
    )
    reporting_acknowledged_at = models.DateTimeField(null=True)
    workspace_removed_at = models.DateTimeField(null=True)
    local_logs_removed_at = models.DateTimeField(null=True)
    documents_removed_at = models.DateTimeField(null=True)
    logs_removed_at = models.DateTimeField(null=True)
    documents_removed = models.PositiveIntegerField(default=0)
    artifacts_removed = models.PositiveIntegerField(default=0)
    local_status = models.CharField(max_length=100, blank=True)


class ReleaseQualification(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    runner = models.ForeignKey(
        "runner_control.RunnerIdentity", on_delete=models.PROTECT
    )
    repository_url = models.URLField(max_length=500)
    scope_digest = models.CharField(max_length=64)
    environment_digest = models.CharField(max_length=64)
    code_revision = models.CharField(max_length=64)
    authorization_note = models.TextField(max_length=2000)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    qualified_at = models.DateTimeField(null=True)
    revoked_at = models.DateTimeField(null=True)
    evidence = models.JSONField(default=dict)
    blocker = models.TextField(default="Controlled live acceptance has not completed.")


class RunnerHealth(models.Model):
    runner = models.OneToOneField(
        "runner_control.RunnerIdentity", on_delete=models.PROTECT
    )
    report = models.JSONField(default=dict)
    received_at = models.DateTimeField(auto_now=True)
