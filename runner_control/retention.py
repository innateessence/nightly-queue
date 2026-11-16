from datetime import timedelta

from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.utils import timezone
from django.utils.crypto import constant_time_compare

from .auth import token_digest
from .models import ContextSnapshot, PlanVersion, Run, RunRetention
from .services import RunConflict, lock_controller, owned_run
from .settings_models import RunnerSettings

PRESERVED_EVIDENCE = {
    "verified_delivery",
    "delivery",
    "validation",
    "progress",
    "recovery",
    "cancellation",
    "finish",
    "release",
}


def policy():
    settings, _ = RunnerSettings.objects.get_or_create(pk=1)
    return {
        "workspace_days": settings.workspace_retention_days,
        "document_days": settings.plan_retention_days,
        "log_days": settings.log_retention_days,
    }


def eligible(run):
    if run.state != Run.State.COMPLETED or not run.finished_at:
        return False
    if run.approvals.filter(decision="pending").exists():
        return False
    retention = RunRetention.objects.filter(run=run).first()
    return bool(retention and retention.reporting_acknowledged_at)


@transaction.atomic
def acknowledge(runner, run_id, claim_token, finish_digest):
    lock_controller(runner)
    run = owned_run(runner, run_id)
    if run.state != Run.State.COMPLETED or not run.finish_digest:
        raise RunConflict(
            "Only confirmed completed reporting can release retention protection."
        )
    if not constant_time_compare(run.claim_digest, token_digest(str(claim_token))):
        raise RunConflict("Reporting acknowledgement has a stale claim.")
    if not constant_time_compare(run.finish_digest, finish_digest):
        raise RunConflict(
            "Reporting acknowledgement does not match the completed result."
        )
    retention, _ = RunRetention.objects.get_or_create(run=run)
    if not retention.reporting_acknowledged_at:
        retention.reporting_acknowledged_at = timezone.now()
        retention.save(update_fields=["reporting_acknowledged_at"])
    return retention


def plan(runner, now=None):
    now = now or timezone.now()
    effective = policy()
    cutoff = now - timedelta(days=effective["workspace_days"])
    candidates = Run.objects.filter(
        runner=runner, state=Run.State.COMPLETED, finished_at__lte=cutoff
    )
    candidates = candidates.filter(retention__reporting_acknowledged_at__isnull=False)
    candidates = candidates.filter(
        retention__workspace_removed_at__isnull=True
    ).order_by("finished_at")[:100]
    workspaces = [str(run.pk) for run in candidates if eligible(run)]
    log_cutoff = now - timedelta(days=effective["log_days"])
    logs = Run.objects.filter(
        runner=runner,
        state=Run.State.COMPLETED,
        finished_at__lte=log_cutoff,
        retention__reporting_acknowledged_at__isnull=False,
        retention__local_logs_removed_at__isnull=True,
    ).order_by("finished_at")[:100]
    return {
        "schema_version": 1,
        "policy": effective,
        "workspaces": workspaces,
        "logs": [str(run.pk) for run in logs if eligible(run)],
    }


@transaction.atomic
def workspace_removed(runner, run_id, status, resource="workspace"):
    if resource not in {"workspace", "logs"}:
        raise ValidationError({"resource": "Choose workspace or logs."})
    lock_controller(runner)
    run = owned_run(runner, run_id)
    cutoff = timezone.now() - timedelta(
        days=policy()["workspace_days" if resource == "workspace" else "log_days"]
    )
    if not eligible(run) or run.finished_at > cutoff:
        raise RunConflict("Run remains protected from workspace retention.")
    if status not in {"removed", "missing"}:
        raise ValidationError({"status": "Report removed or missing."})
    retention = RunRetention.objects.select_for_update().get(run=run)
    if resource == "logs":
        retention.local_logs_removed_at = (
            retention.local_logs_removed_at or timezone.now()
        )
    else:
        retention.workspace_removed_at = (
            retention.workspace_removed_at or timezone.now()
        )
        retention.local_status = status
    retention.save()
    return retention


@transaction.atomic
def purge_run(run_id, now=None):
    # Controller lock and a fresh eligibility check are mandatory for the immutable-document escape hatch.
    lock_controller()
    run = Run.objects.select_for_update().get(pk=run_id)
    if not eligible(run):
        raise RunConflict(
            "Active, unacknowledged, or approval-pending runs cannot be purged."
        )
    now = now or timezone.now()
    effective = policy()
    retention = RunRetention.objects.select_for_update().get(run=run)
    if run.finished_at <= now - timedelta(days=effective["document_days"]):
        if not run.approvals.exists() and retention.documents_removed_at is None:
            plans = PlanVersion.objects.filter(run=run)
            contexts = ContextSnapshot.objects.filter(run=run)
            count = plans.count() + contexts.count()
            # Reverse version references are removed together; external references still PROTECT.
            models.QuerySet.update(plans, supersedes=None)
            models.QuerySet.delete(plans)
            models.QuerySet.delete(contexts)
            retention.documents_removed = count
            retention.documents_removed_at = now
    if run.finished_at <= now - timedelta(days=effective["log_days"]):
        if retention.logs_removed_at is None:
            entries = run.evidence.exclude(kind__in=PRESERVED_EVIDENCE)
            retention.artifacts_removed = entries.count()
            entries.delete()
            retention.logs_removed_at = now
    retention.save()
    return retention
