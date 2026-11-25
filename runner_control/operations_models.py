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


class RunnerHealth(models.Model):
    runner = models.OneToOneField(
        "runner_control.RunnerIdentity", on_delete=models.PROTECT
    )
    report = models.JSONField(default=dict)
    received_at = models.DateTimeField(auto_now=True)
