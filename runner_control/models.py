import uuid

from django.db import models


class RunnerIdentity(models.Model):
    name = models.SlugField(max_length=100, unique=True)
    token_digest = models.CharField(max_length=64, editable=False)
    enabled = models.BooleanField(default=True)
    controller_digest = models.CharField(max_length=64, blank=True, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name


class RunnerController(models.Model):
    # An existing singleton row serializes all runner ownership transitions.
    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    generation = models.PositiveBigIntegerField(default=0)


class Run(models.Model):
    class State(models.TextChoices):
        ACTIVE = "active", "Active"
        COMPLETED = "completed", "Completed"
        EMPTY = "empty", "Empty queue"
        OBSOLETE = "obsolete", "Ownership obsolete"

    class Stage(models.TextChoices):
        CLAIMED = "claimed", "Claimed"
        CONTEXT = "context", "Context"
        RESEARCH = "research", "Research"
        PLANNING = "planning", "Planning"
        IMPLEMENTATION = "implementation", "Implementation"
        VALIDATION = "validation", "Validation"
        DELIVERY = "delivery", "Delivery"
        WAITING = "waiting", "Waiting"
        FINISHED = "finished", "Finished"
        INTAKE = "intake", "Intake"
        SPECIFICATION = "specification", "Specification"
        BUILD = "build", "Build"
        CHECKS = "checks", "Checks"
        REVIEW = "review", "Review"
        DECISION = "decision", "Decision"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    runner = models.ForeignKey(
        RunnerIdentity, on_delete=models.PROTECT, related_name="runs"
    )
    item = models.ForeignKey(
        "work_queue.QueueItem", on_delete=models.PROTECT, null=True, related_name="runs"
    )
    state = models.CharField(max_length=12, choices=State.choices)
    active_slot = models.PositiveSmallIntegerField(
        null=True, unique=True, editable=False
    )
    stage = models.CharField(
        max_length=20, choices=Stage.choices, default=Stage.CLAIMED
    )
    wait_reason = models.TextField(blank=True)
    configuration_revision = models.CharField(max_length=64, blank=True)
    configuration_snapshot = models.JSONField(default=dict, blank=True)
    source_snapshot = models.JSONField(default=dict, blank=True, editable=False)
    context_reference = models.CharField(max_length=200, blank=True)
    plan_reference = models.CharField(max_length=200, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    result_status = models.CharField(max_length=9, blank=True)
    result_url = models.URLField(max_length=500, blank=True)
    error_summary = models.TextField(blank=True)
    finish_digest = models.CharField(max_length=64, blank=True, editable=False)
    claim_digest = models.CharField(max_length=64, blank=True, editable=False)
    cancellation_requested_at = models.DateTimeField(null=True, blank=True)
    cancellation_confirmed_at = models.DateTimeField(null=True, blank=True)
    cancellation_reason = models.CharField(max_length=2000, blank=True)
    native_status = models.JSONField(default=dict, blank=True, editable=False)
    heartbeat_at = models.DateTimeField(null=True, blank=True, editable=False)
    native_outcome = models.CharField(max_length=20, blank=True, editable=False)
    retry_requested_at = models.DateTimeField(null=True, blank=True, editable=False)

    class Meta:
        ordering = ["created_at", "pk"]
        constraints = [
            models.CheckConstraint(
                check=models.Q(
                    state="active",
                    active_slot=1,
                    active_slot__isnull=False,
                    item__isnull=False,
                )
                | (~models.Q(state="active") & models.Q(active_slot__isnull=True)),
                name="runner_active_slot",
            ),
        ]


class RunEvidence(models.Model):
    run = models.ForeignKey(Run, on_delete=models.PROTECT, related_name="evidence")
    operation_id = models.UUIDField()
    kind = models.CharField(max_length=80)
    payload = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["run", "operation_id"], name="runner_evidence_operation"
            )
        ]


from .settings_models import ProjectRunnerSettings, RunnerSettings  # noqa: F401, E402
from .settings_models import RunnerSchedule  # noqa: F401, E402
from .gate_models import RunnerGateEvidence  # noqa: F401, E402
from .plan_models import ContextSnapshot, PlanVersion  # noqa: F401, E402
from .factory_models import FactoryArtifact  # noqa: F401, E402
from .operations_models import RunRetention  # noqa: F401, E402
from .operations_models import RunnerHealth  # noqa: F401, E402
