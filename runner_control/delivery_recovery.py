"""One audited continuation of a terminated, validated publication."""

from datetime import timedelta
from uuid import uuid4

from django.db import transaction
from django.utils import timezone

from vibearr_runner import native_contract as contract
from vibearr_runner.delivery import DeliveryError
from vibearr_runner.merge_github import exact_pull
from work_queue.models import QueueItem

from .auth import token_digest
from .models import NativeDeliveryRecovery, Run, RunRetention
from .native_services import authorize, native_policy, require_termination
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
    retention = validate_failed(run, expected)
    publication = run.evidence.filter(
        kind="native_delivery", payload__pull_request__isnull=False
    ).last()
    if publication is None or publication.payload["pull_request"] is None:
        raise RunConflict("Recovery requires an existing owned pull request.")
    if (
        publication.payload["publication_id"] != expected["expected_publication_id"]
        or publication.payload["workspace"]["commit_sha"]
        != expected["expected_head_sha"]
    ):
        raise RunConflict(
            "Recovery publication or candidate differs from the expected identity."
        )
    resume = resume_document(run, publication)
    archive = {
        "schema_version": 1,
        "generation": 1,
        "state": run.state,
        "stage": run.stage,
        "result_status": run.result_status,
        "native_outcome": run.native_outcome,
        "result_url": run.result_url,
        "item_result_url": run.item.result_url,
        "error_summary": run.error_summary,
        "finish_digest": run.finish_digest,
        "claim_digest": run.claim_digest,
        "finished_at": run.finished_at.isoformat(),
        "native_status": run.native_status,
        "reporting_acknowledged_at": retention.reporting_acknowledged_at.isoformat(),
        "publication_operation_id": str(publication.operation_id),
        "original_deadline": resume["deadline"],
        "consumed_repairs": resume["attempt"],
    }
    token = uuid4()
    # Changes remain transactional until current configuration and provider facts pass.
    run.state, run.active_slot, run.stage = Run.State.ACTIVE, 1, Run.Stage.DELIVERY
    run.claim_digest = token_digest(str(token))
    run.item.status, run.item.claim_token = QueueItem.Status.RUNNING, token
    run.item.result_url = ""
    authorize(run)
    from .merge_services import provider

    try:
        github = provider(run)
        raw = github.pull(
            publication.payload["workspace"]["repository"],
            publication.payload["pull_request"]["number"],
        )
        exact_pull(
            raw,
            publication.payload["workspace"],
            publication.payload["pull_request"]["number"],
        )
        if (
            raw.get("state") != "open"
            or raw.get("draft") is not False
            or raw.get("merged")
        ):
            raise RunConflict(
                "Recovery requires the original open unmerged ready pull request."
            )
        github.policy(
            publication.payload["workspace"],
            run.configuration_snapshot["effective"]["merge_profile"],
        )
    except DeliveryError as exc:
        raise RunConflict(str(exc)) from None
    recovery = NativeDeliveryRecovery.objects.create(
        run=run,
        operation_id=operation_id,
        request=expected,
        archive=archive,
        resume=resume,
        claim_token=token,
    )
    run.finished_at = None
    run.result_status = run.result_url = run.error_summary = run.finish_digest = (
        run.native_outcome
    ) = run.wait_reason = ""
    run.item.finished_at = None
    run.item.error_summary = run.item.result_url = ""
    run.item.save()
    run.save()
    retention.reporting_acknowledged_at = None
    retention.save(update_fields=["reporting_acknowledged_at"])
    return run, recovery
