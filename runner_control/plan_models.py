import uuid

from django.core.exceptions import ValidationError
from django.db import models


class ImmutableDocument(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    operation_id = models.UUIDField()
    document = models.JSONField()
    digest = models.CharField(max_length=64)
    reason = models.CharField(max_length=2000)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError("Saved context and plan versions are immutable.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Saved context and plan versions are immutable.")


class ContextSnapshot(ImmutableDocument):
    run = models.ForeignKey(
        "runner_control.Run", on_delete=models.PROTECT, related_name="contexts"
    )
    sequence = models.PositiveIntegerField()
    inspected_base_commit = models.CharField(max_length=64)
    discussion_digest = models.CharField(max_length=64)
    changes = models.JSONField(default=list)

    class Meta:
        ordering = ["sequence"]
        constraints = [
            models.UniqueConstraint(
                fields=["run", "operation_id"], name="runner_context_operation"
            ),
            models.UniqueConstraint(
                fields=["run", "sequence"], name="runner_context_sequence"
            ),
        ]


class PlanVersion(ImmutableDocument):
    run = models.ForeignKey(
        "runner_control.Run", on_delete=models.PROTECT, related_name="plans"
    )
    context = models.ForeignKey(
        ContextSnapshot, on_delete=models.PROTECT, related_name="plans"
    )
    version = models.PositiveIntegerField()
    supersedes = models.ForeignKey("self", null=True, on_delete=models.PROTECT)

    class Meta:
        ordering = ["version"]
        constraints = [
            models.UniqueConstraint(
                fields=["run", "operation_id"], name="runner_plan_operation"
            ),
            models.UniqueConstraint(
                fields=["run", "version"], name="runner_plan_version"
            ),
        ]
