from datetime import timedelta
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.utils import timezone
from django.utils.crypto import constant_time_compare

from .auth import token_digest
from .models import ContextSnapshot, PlanVersion, Run, RunRetention
from .services import RunConflict, lock_controller, owned_run
from .configuration import runner_settings
from vibearr_runner.native_contract import ARTIFACT_KINDS, digest

PRESERVED_EVIDENCE = {
    "verified_delivery",
    "delivery",
    "validation",
    "progress",
    "recovery",
    "cancellation",
    "finish",
    "release",
    "factory_retention",
    "publication_observation",
}
PRESERVED_EVIDENCE.update(ARTIFACT_KINDS)


def policy(runner=None):
    settings = runner_settings(runner)
    return {
        "workspace_days": settings.workspace_retention_days,
        "document_days": settings.plan_retention_days,
        "log_days": settings.log_retention_days,
    }


def reported_terminal(run):
    return run.state == Run.State.COMPLETED or (
        run.state == Run.State.OBSOLETE
        and run.configuration_snapshot.get("execution_kind") == "native"
        and bool(run.finish_digest)
    )


def eligible(run):
    if not reported_terminal(run) or not run.finished_at:
        return False
    if run.approvals.filter(decision="pending").exists():
        return False
    retention = RunRetention.objects.filter(run=run).first()
    return bool(retention and retention.reporting_acknowledged_at)


@transaction.atomic
def acknowledge(runner, run_id, claim_token, finish_digest):
    lock_controller(runner)
    run = owned_run(runner, run_id)
    if not reported_terminal(run) or not run.finish_digest:
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


def eligible_runs(runner):
    return Run.objects.filter(
        models.Q(state=Run.State.COMPLETED)
        | (
            models.Q(
                state=Run.State.OBSOLETE,
                configuration_snapshot__execution_kind="native",
            )
            & ~models.Q(finish_digest="")
        ),
        runner=runner,
        finished_at__isnull=False,
        retention__reporting_acknowledged_at__isnull=False,
    ).exclude(approvals__decision="pending")


def purge_candidates(runner, now=None):
    now = now or timezone.now()
    effective = policy(runner)
    documents = models.Q(
        finished_at__lte=now - timedelta(days=effective["document_days"]),
        retention__documents_removed_at__isnull=True,
        approvals__isnull=True,
    )
    logs = models.Q(
        finished_at__lte=now - timedelta(days=effective["log_days"]),
        retention__logs_removed_at__isnull=True,
    )
    return (
        eligible_runs(runner)
        .filter(documents | logs)
        .distinct()
        .order_by("finished_at")[:100]
    )


def plan(runner, now=None):
    now = now or timezone.now()
    effective = policy(runner)
    cutoff = now - timedelta(days=effective["workspace_days"])
    candidates = (
        eligible_runs(runner)
        .filter(finished_at__lte=cutoff, retention__workspace_removed_at__isnull=True)
        .order_by("finished_at")[:100]
    )
    log_cutoff = now - timedelta(days=effective["log_days"])
    logs = (
        eligible_runs(runner)
        .filter(
            finished_at__lte=log_cutoff, retention__local_logs_removed_at__isnull=True
        )
        .order_by("finished_at")[:100]
    )
    return {
        "schema_version": 1,
        "policy": effective,
        "workspaces": [str(run.pk) for run in candidates],
        "logs": [str(run.pk) for run in logs],
    }


@transaction.atomic
def workspace_removed(runner, run_id, status, resource="workspace"):
    if resource not in {"workspace", "logs"}:
        raise ValidationError({"resource": "Choose workspace or logs."})
    lock_controller(runner)
    run = owned_run(runner, run_id)
    cutoff = timezone.now() - timedelta(
        days=policy(runner)["workspace_days" if resource == "workspace" else "log_days"]
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
    effective = policy(run.runner)
    retention = RunRetention.objects.select_for_update().get(run=run)
    if run.finished_at <= now - timedelta(days=effective["document_days"]):
        if not run.approvals.exists() and retention.documents_removed_at is None:
            plans = PlanVersion.objects.filter(run=run)
            contexts = ContextSnapshot.objects.filter(run=run)
            artifacts = run.factory_artifacts.all()
            summaries = list(artifacts.values("sequence", "role", "round", "digest"))
            count = plans.count() + contexts.count() + len(summaries)
            if summaries:
                run.evidence.create(
                    operation_id=uuid4(),
                    kind="factory_retention",
                    payload={
                        "schema_version": 1,
                        "count": len(summaries),
                        "digest": digest(summaries),
                    },
                )
            # Reverse version references are removed together; external references still PROTECT.
            models.QuerySet.delete(artifacts)
            models.QuerySet.update(plans, supersedes=None)
            models.QuerySet.delete(plans)
            models.QuerySet.delete(contexts)
            retention.documents_removed = count
            retention.documents_removed_at = now
            if run.configuration_snapshot.get("execution_kind") == "native":
                original = run.source_snapshot
                tombstone = {"purged": True, "digest": digest(original)}
                if run.native_outcome == "delivered":
                    for field in ("repository_id", "issue_number"):
                        if type(original.get(field)) is int:
                            tombstone[field] = original[field]
                Run.objects.filter(pk=run.pk).update(source_snapshot=tombstone)
                from work_queue.models import QueueItem

                newer = Run.objects.filter(
                    item=run.item, created_at__gt=run.created_at
                ).exclude(state=Run.State.OBSOLETE)
                if not newer.exists() and run.item.source_snapshot == original:
                    QueueItem.objects.filter(
                        pk=run.item_id, source_snapshot=original
                    ).update(source_snapshot=tombstone)
    if run.finished_at <= now - timedelta(days=effective["log_days"]):
        if retention.logs_removed_at is None:
            entries = run.evidence.exclude(kind__in=PRESERVED_EVIDENCE)
            retention.artifacts_removed = entries.count()
            entries.delete()
            retention.logs_removed_at = now
            if run.native_status:
                status = dict(run.native_status, detail="")
                Run.objects.filter(pk=run.pk).update(native_status=status)
    retention.save()
    return retention
