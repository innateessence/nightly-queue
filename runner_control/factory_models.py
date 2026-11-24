from django.db import models

from .plan_models import ImmutableDocument


class FactoryArtifact(ImmutableDocument):
    run = models.ForeignKey(
        "runner_control.Run", on_delete=models.PROTECT, related_name="factory_artifacts"
    )
    context = models.ForeignKey(
        "runner_control.ContextSnapshot",
        on_delete=models.PROTECT,
        related_name="factory_artifacts",
    )
    specification = models.ForeignKey(
        "runner_control.PlanVersion",
        null=True,
        on_delete=models.PROTECT,
        related_name="factory_artifacts",
    )
    sequence = models.PositiveIntegerField()
    role = models.CharField(max_length=24)
    round = models.PositiveSmallIntegerField()

    class Meta:
        ordering = ["sequence"]
        constraints = [
            models.UniqueConstraint(
                fields=["run", "operation_id"], name="runner_factory_operation"
            ),
            models.UniqueConstraint(
                fields=["run", "sequence"], name="runner_factory_sequence"
            ),
            models.UniqueConstraint(
                fields=["run", "context", "round", "role"],
                name="runner_factory_role_round",
            ),
        ]
