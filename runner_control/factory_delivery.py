from vibearr_runner.delivery import DeliveryError
from vibearr_runner.factory_delivery import (
    factory_enabled,
    checked_factory_workspace as checked_workspace,
    validate_routing as checked_routing,
)

from .services import RunConflict


def validate_delivery_policy(run, intent):
    policy = run.configuration_snapshot.get("execution_policy", {})
    if policy.get("delivery_mode") == "merge" or intent.get("kind") == "merge":
        raise RunConflict(
            "Automatic merge is disabled; select pull-request delivery for human review."
        )


def validate_routing(*args, **kwargs):
    try:
        return checked_routing(*args, **kwargs)
    except DeliveryError as exc:
        raise RunConflict(str(exc)) from None


def checked_factory_workspace(*args, **kwargs):
    try:
        return checked_workspace(*args, **kwargs)
    except DeliveryError as exc:
        raise RunConflict(str(exc)) from None


def validate_factory_workspace(run, workspace, context):
    if not factory_enabled(run.configuration_snapshot):
        return None
    from .plan_models import PlanVersion

    plan = PlanVersion.objects.filter(
        pk=run.plan_reference, run=run, context=context
    ).first()
    if plan is None or not hasattr(run, "factory_artifacts"):
        raise RunConflict(
            "Publication requires its current saved factory specification."
        )
    records = [
        {
            "id": str(entry.pk),
            "sequence": entry.sequence,
            "document": entry.document,
            "digest": entry.digest,
        }
        for entry in run.factory_artifacts.order_by("sequence")[:1001]
    ]
    evidence = list(
        run.evidence.filter(kind__in=["native_workspace", "native_validation"])
        .order_by("pk")
        .values("operation_id", "kind", "payload")[:2001]
    )
    return checked_factory_workspace(
        run.configuration_snapshot,
        {"id": str(plan.pk), "digest": plan.digest, "document": plan.document},
        {"id": str(context.pk), "digest": context.digest},
        workspace,
        records,
        evidence,
        str(run.pk),
    )
