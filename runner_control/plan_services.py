from datetime import timedelta

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from coding_runner.context import ContextError, digest, discussion_digest
from coding_runner.plans import revision, validate_context, validate_plan
from work_queue.api import item_data

from .models import ContextSnapshot, PlanVersion, Run
from .services import (
    RunConflict,
    append_evidence,
    lock_controller,
    owned_run,
    verify_active,
)

MAX_DOCUMENTS = 200


def claimed_item(run):
    item = item_data(run.item)
    item.update(
        repository_url=run.item.repository_url, base_branch=run.item.base_branch
    )
    return item


def checked(operation):
    try:
        return operation()
    except ContextError as exc:
        raise ValidationError({"document": str(exc)}) from None


def previous_operation(model, run, operation_id, document, reason, **extra):
    previous = model.objects.filter(run=run, operation_id=operation_id).first()
    if previous is None:
        if model.objects.filter(run=run).count() >= MAX_DOCUMENTS:
            raise RunConflict("Run document version limit reached.")
        return None
    same = previous.digest == digest(document) and previous.reason == reason
    if not same or any(
        str(getattr(previous, key)) != str(value) for key, value in extra.items()
    ):
        raise RunConflict("Document operation ID already contains different content.")
    return previous


def locked_run(runner, run_id, claim_token):
    lock_controller(runner)
    run = owned_run(runner, run_id)
    verify_active(run, claim_token)
    return run


@transaction.atomic
def save_context(
    *, runner, run_id, claim_token, operation_id, document, base_commit, reason
):
    run = locked_run(runner, run_id, claim_token)
    checked(lambda: revision(base_commit))
    checked(lambda: validate_context(document, claimed_item(run)))
    previous = previous_operation(
        ContextSnapshot,
        run,
        operation_id,
        document,
        reason,
        inspected_base_commit=base_commit,
    )
    if previous is not None:
        return previous
    latest = run.contexts.last()
    if latest is not None and not reason.strip():
        raise ValidationError({"reason": "Describe why the context was refreshed."})
    changes = []
    discussion = discussion_digest(document)
    if latest and latest.discussion_digest != discussion:
        changes.append("discussion")
    if latest and latest.inspected_base_commit != base_commit:
        changes.append("base_commit")
    snapshot = ContextSnapshot.objects.create(
        run=run,
        operation_id=operation_id,
        sequence=latest.sequence + 1 if latest else 1,
        document=document,
        digest=digest(document),
        discussion_digest=discussion,
        inspected_base_commit=base_commit,
        reason=reason,
        changes=changes,
    )
    run.context_reference = str(snapshot.pk)
    run.save(update_fields=["context_reference", "updated_at"])
    return snapshot


@transaction.atomic
def save_plan(*, runner, run_id, claim_token, operation_id, document, reason):
    run = locked_run(runner, run_id, claim_token)
    latest_context = run.contexts.last()
    if latest_context is None:
        raise RunConflict("Save complete context before a plan.")
    checked(
        lambda: validate_plan(
            document,
            latest_context.document,
            str(latest_context.pk),
            latest_context.inspected_base_commit,
        )
    )
    previous = previous_operation(PlanVersion, run, operation_id, document, reason)
    if previous is not None:
        return previous
    latest = run.plans.last()
    if latest is not None and not reason.strip():
        raise ValidationError(
            {"reason": "Describe the material plan revision or assessment."}
        )
    plan = PlanVersion.objects.create(
        run=run,
        context=latest_context,
        operation_id=operation_id,
        version=latest.version + 1 if latest else 1,
        supersedes=latest,
        document=document,
        digest=digest(document),
        reason=reason,
    )
    run.plan_reference = str(plan.pk)
    run.save(update_fields=["plan_reference", "updated_at"])
    return plan


def valid_handoff(run, *, require_refresh=True):
    plan = run.plans.last()
    current = run.contexts.last()
    if plan is None or current is None:
        raise RunConflict(
            "Implementation requires a saved valid plan and complete context."
        )
    if run.plan_reference != str(plan.pk) or run.context_reference != str(current.pk):
        raise RunConflict("Context or plan reference is not the latest saved version.")
    checked(
        lambda: validate_plan(
            plan.document,
            plan.context.document,
            str(plan.context_id),
            plan.context.inspected_base_commit,
        )
    )
    if plan.document["blockers"]:
        raise RunConflict("Unresolved plan blockers prevent implementation.")
    if (
        current.discussion_digest != plan.context.discussion_digest
        or current.inspected_base_commit != plan.context.inspected_base_commit
    ):
        raise RunConflict(
            "Discussion or base changed; save a revised plan with an assessment reason."
        )
    cutoff = timezone.now() - timedelta(minutes=5)
    if require_refresh and (
        current.created_at < plan.created_at or current.created_at < cutoff
    ):
        raise RunConflict(
            "Refresh complete discussion and inspect the base before handoff."
        )
    return plan, current


@transaction.atomic
def handoff(*, runner, run_id, claim_token, operation_id, context_id, base_commit):
    run = locked_run(runner, run_id, claim_token)
    plan, current = valid_handoff(run)
    if (
        str(current.pk) != str(context_id)
        or current.inspected_base_commit != base_commit
    ):
        raise RunConflict(
            "Handoff must use the latest refreshed context and inspected base."
        )
    payload = {
        "plan_id": str(plan.pk),
        "context_id": str(current.pk),
        "base_commit": base_commit,
    }
    append_evidence(run, operation_id, "plan_handoff", payload)
    return plan, current


def require_saved_plan(run):
    plan, current = valid_handoff(run)
    evidence = run.evidence.filter(kind="plan_handoff").last()
    if evidence is None or evidence.payload != {
        "plan_id": str(plan.pk),
        "context_id": str(current.pk),
        "base_commit": current.inspected_base_commit,
    }:
        raise RunConflict(
            "Verify the refreshed saved-plan handoff before implementation."
        )
