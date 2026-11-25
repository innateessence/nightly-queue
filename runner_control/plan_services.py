from django.core.exceptions import ValidationError
from django.db import transaction

from credentials.redaction import redact_run_payload

from vibearr_runner.context import digest
from vibearr_runner.native_contract import NativeContractError, sha

from .models import ContextSnapshot, PlanVersion
from .services import (
    RunConflict,
    lock_controller,
    owned_run,
    verify_active,
)

MAX_DOCUMENTS = 200


def checked(operation):
    try:
        return operation()
    except NativeContractError as exc:
        raise ValidationError({"document": str(exc)}) from None


def require_safe_document(run, document):
    if redact_run_payload(run, document) != document:
        raise ValidationError(
            {
                "document": "Saved documents must not contain selected credential material."
            }
        )


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
    *,
    runner,
    run_id,
    claim_token,
    operation_id,
    document,
    base_commit,
    reason,
    resolved_base_branch,
):
    run = locked_run(runner, run_id, claim_token)
    require_safe_document(run, document)
    reason = redact_run_payload(run, reason)
    if redact_run_payload(run, resolved_base_branch) != resolved_base_branch:
        raise ValidationError(
            {
                "resolved_base_branch": "Base branch contains selected credential material."
            }
        )
    checked(lambda: sha(base_commit, git=True))
    if not resolved_base_branch.strip() or (
        run.item.base_branch and resolved_base_branch != run.item.base_branch
    ):
        raise ValidationError(
            {"resolved_base_branch": "Record the actual resolved base branch."}
        )
    previous = previous_operation(
        ContextSnapshot,
        run,
        operation_id,
        document,
        reason,
        inspected_base_commit=base_commit,
        resolved_base_branch=resolved_base_branch,
    )
    if previous is not None:
        return previous
    from .native_services import authorize, lock_scope, validate_context_binding

    run, configuration = lock_scope(run, claim_token)
    authorize(run, configuration)
    checked(
        lambda: validate_context_binding(
            run, document, base_commit, resolved_base_branch
        )
    )
    latest = run.contexts.last()
    if latest is not None and not reason.strip():
        raise ValidationError({"reason": "Describe why the context was refreshed."})
    changes = []
    discussion = digest(document["source"])
    if latest and latest.discussion_digest != discussion:
        changes.append("discussion")
    if latest and latest.resolved_base_branch != resolved_base_branch:
        changes.append("resolved_base_branch")
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
        resolved_base_branch=resolved_base_branch,
        reason=reason,
        changes=changes,
    )
    run.context_reference = str(snapshot.pk)
    run.save(update_fields=["context_reference", "updated_at"])
    return snapshot


@transaction.atomic
def save_plan(*, runner, run_id, claim_token, operation_id, document, reason):
    run = locked_run(runner, run_id, claim_token)
    require_safe_document(run, document)
    reason = redact_run_payload(run, reason)
    previous = previous_operation(PlanVersion, run, operation_id, document, reason)
    if previous is not None:
        return previous
    latest_context = run.contexts.last()
    if latest_context is None:
        raise RunConflict("Save complete context before a plan.")
    require_safe_document(run, latest_context.document)
    from .native_services import validate_plan_binding

    checked(lambda: validate_plan_binding(run, document, latest_context))
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
