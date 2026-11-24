"""Historical delivery recovery readback never restores completed claims."""

from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from vibearr_runner import native_contract as contract
from work_queue.models import QueueItem

from .models import NativeDeliveryRecovery, Run, RunRetention
from .native_services import native_policy, require_termination
from .services import RunConflict, lock_controller, owned_run


def resume_document(run, publication):
    workspace = run.evidence.filter(
        operation_id=publication.payload["workspace_id"],
        kind__in=["native_workspace", "native_revision"],
    ).get()
    candidate = workspace.payload
    if candidate["workspace"] != publication.payload["workspace"]:
        raise RunConflict("Recovery publication differs from its inspected workspace.")
    latest = run.evidence.filter(
        kind__in=["native_workspace", "native_revision"]
    ).last()
    if latest.pk != workspace.pk:
        raise RunConflict("Recovery requires the latest inspected candidate.")
    agent_intent = run.evidence.get(
        operation_id=candidate["agent_intent_id"], kind="native_intent"
    )
    agent = run.evidence.get(
        kind="native_agent", payload__intent_id=str(agent_intent.operation_id)
    )
    validation = run.evidence.filter(
        kind="native_validation",
        payload__state="passed",
        payload__tree_sha=candidate["workspace"]["tree_sha"],
        payload__source_digest=candidate["source_digest"],
    ).last()
    if validation is None or agent.payload["successful"] is not True:
        raise RunConflict(
            "Recovery requires successful original agent and exact validation."
        )
    ctx = run.contexts.get(pk=run.context_reference)
    plan = run.plans.get(pk=run.plan_reference, context=ctx)
    intents = {
        str(e.operation_id): e.payload
        for e in run.evidence.filter(kind="native_intent")
    }
    deadline = run.created_at + timedelta(
        seconds=run.configuration_snapshot["global"]["task_timeout_seconds"]
    )
    if timezone.now() >= deadline:
        raise RunConflict(
            "Original native task deadline has expired; recovery cannot extend it."
        )
    attempt = max(
        [
            run.native_status.get("repair_count", 0),
            *[i["attempt"] for i in intents.values()],
        ]
    )
    if attempt != candidate["attempt"]:
        raise RunConflict(
            "A later unresolved candidate attempt prevents delivery-only recovery."
        )
    return {
        "schema_version": 1,
        "run_id": str(run.pk),
        "phase": "recovery_delivery",
        "attempt": attempt,
        "deadline": deadline.isoformat(),
        "delivery_recovery_generation": 1,
        "context": {"id": str(ctx.pk), "digest": ctx.digest, "document": ctx.document},
        "plan": {"id": str(plan.pk), "digest": plan.digest, "document": plan.document},
        "repository": {
            "repository_id": run.item.project.github_repository_id,
            "owner_id": run.item.project.github_owner_id,
            "repository_url": run.item.project.repository_url,
            "default_branch": run.item.project.github_default_branch,
        },
        "repository_at_checkout": {
            **ctx.document["repository"],
            "default_branch": run.configuration_snapshot["source_policy"][
                "default_branch"
            ],
        },
        "workspace_id": str(workspace.operation_id),
        "candidate": candidate,
        "intents": intents,
        "agent": {
            "intent_id": str(agent_intent.operation_id),
            "session": agent_intent.payload["data"]["session"],
            "agent_version": agent.payload["agent_version"],
            "session_id": agent.payload["session_id"],
        },
        "validation": {"evidence": contract.validation_evidence(validation.payload)},
        "status_sequence": run.native_status.get("sequence", 0),
        "ci_deadline": min(
            deadline,
            publication.created_at
            + timedelta(
                seconds=run.configuration_snapshot["effective"]["merge_profile"][
                    "ci_timeout_seconds"
                ]
            ),
        ).isoformat(),
        "delivery": {
            **publication.payload,
            "checkpoint": "pr_verified",
            "intents": {
                intents[k]["data"]["stage"]: k
                for k in publication.payload["intent_ids"]
            },
        },
    }


def validate_failed(run, expected):
    if (
        run.state != Run.State.COMPLETED
        or run.result_status != "failed"
        or run.native_outcome != "failed"
    ):
        raise RunConflict("Recovery requires a completed failed native delivery.")
    if run.finish_digest != expected["expected_finish_digest"] or not run.finish_digest:
        raise RunConflict("Recovery expected a different terminal result.")
    if (
        native_policy(run).delivery_mode != "merge"
        or run.cancellation_requested_at
        or run.retry_requested_at
    ):
        raise RunConflict("Only uncanceled merge-mode delivery can recover.")
    if Run.objects.filter(state=Run.State.ACTIVE).exists():
        raise RunConflict("Another active run owns execution.")
    latest = Run.objects.filter(item=run.item).order_by("created_at", "pk").last()
    if latest.pk != run.pk or run.item.status != QueueItem.Status.FAILED:
        raise RunConflict("Recovery no longer owns the latest failed queue item.")
    retention = RunRetention.objects.select_for_update().filter(run=run).first()
    if (
        retention is None
        or not retention.reporting_acknowledged_at
        or any(
            (
                retention.workspace_removed_at,
                retention.documents_removed_at,
                retention.local_logs_removed_at,
            )
        )
    ):
        raise RunConflict(
            "Recovery requires positive reporting acknowledgement and retained state."
        )
    require_termination(run)
    from .merge_services import require_merge_terminal

    require_merge_terminal(run)
    if run.evidence.filter(kind__in=["native_merge", "native_completion"]).exists():
        raise RunConflict(
            "An already merged publication requires factual reconciliation."
        )
    return retention


@transaction.atomic
def recover_delivery(*, runner, run_id, operation_id, expected):
    lock_controller(runner)
    run = owned_run(runner, run_id)
    run.item = (
        QueueItem.objects.select_for_update()
        .select_related("project")
        .get(pk=run.item_id)
    )
    previous = NativeDeliveryRecovery.objects.filter(run=run).first()
    if previous:
        if previous.operation_id != operation_id or previous.request != expected:
            raise RunConflict(
                "This run already used its one delivery recovery generation."
            )
        return run, previous
    if NativeDeliveryRecovery.objects.filter(operation_id=operation_id).exists():
        raise RunConflict("Recovery operation already belongs to another run.")
    raise RunConflict(
        "Automatic merge is disabled; repair requires an explicit new attempt retaining the original pull request."
    )
