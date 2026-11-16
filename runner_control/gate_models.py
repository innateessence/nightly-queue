from django.db import models


class RunnerGateEvidence(models.Model):
    runner = models.OneToOneField(
        "runner_control.RunnerIdentity",
        on_delete=models.PROTECT,
        related_name="gate_evidence",
    )
    report = models.JSONField()
    checked_at = models.DateTimeField()
    received_at = models.DateTimeField(auto_now=True)
