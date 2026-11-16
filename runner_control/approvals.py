import json

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from coding_runner.approvals import ActionSpec

from .models import ActionApproval, Run
from .services import (
    RunConflict,
    RunNotFound,
    lock_controller,
    owned_run,
    verify_active,
)


def parse_action(data):
    try:
        return ActionSpec.from_data(data)
    except (ValueError, TypeError):
        raise ValidationError(
            {"action": "Supply a valid, complete command and provenance binding."}
        ) from None


def verify_workspace(action, run):
    if action.workspace_id != str(run.pk):
        raise RunConflict("Action workspace does not match the claimed run.")


def approval_data(approval):
    return {
        "id": str(approval.pk),
        "run_id": str(approval.run_id),
        "operation_id": str(approval.operation_id),
        "action": approval.action,
        "binding_digest": approval.binding_digest,
        "decision": approval.decision,
        "decision_note": approval.decision_note,
        "decided_at": approval.decided_at.isoformat() if approval.decided_at else None,
        "created_at": approval.created_at.isoformat(),
        "consumed_at": (
            approval.consumed_at.isoformat() if approval.consumed_at else None
        ),
        "execution_id": str(approval.execution_id) if approval.execution_id else None,
    }


@transaction.atomic
def request_approval(*, runner, run_id, claim_token, operation_id, action):
    lock_controller(runner)
    run = owned_run(runner, run_id)
    verify_active(run, claim_token)
    parsed = parse_action(action)
    verify_workspace(parsed, run)
    previous = ActionApproval.objects.filter(run=run, operation_id=operation_id).first()
    if previous is not None:
        if previous.binding_digest != parsed.binding_digest:
            raise RunConflict("This approval operation already has different content.")
        return previous
    if run.approvals.count() >= 1000:
        raise RunConflict("Run approval limit reached.")
    approval = ActionApproval.objects.create(
        run=run,
        operation_id=operation_id,
        action=parsed.to_data(),
        binding_digest=parsed.binding_digest,
    )
    run.stage = Run.Stage.WAITING
    run.wait_reason = (
        "Human approval required for a command from " + parsed.source_kind + "."
    )
    run.save(update_fields=["stage", "wait_reason", "updated_at"])
    return approval


def owned_approval(runner, run_id, approval_id):
    run = owned_run(runner, run_id)
    try:
        return run.approvals.get(pk=approval_id)
    except ActionApproval.DoesNotExist:
        raise RunNotFound("Approval not found.") from None


@transaction.atomic
def consume_approval(
    *, runner, run_id, claim_token, approval_id, binding_digest, execution_id
):
    lock_controller(runner)
    run = owned_run(runner, run_id)
    verify_active(run, claim_token)
    approval = owned_approval(runner, run_id, approval_id)
    if approval.binding_digest != binding_digest:
        raise RunConflict("Approval binding changed; request a new human decision.")
    parsed = parse_action(approval.action)
    verify_workspace(parsed, run)
    if parsed.binding_digest != binding_digest:
        raise RunConflict("Stored action changed; request a new human decision.")
    if approval.decision != ActionApproval.Decision.APPROVED:
        raise RunConflict("This action has not been approved by a human.")
    if approval.consumed_at is not None:
        return approval, False
    approval.execution_id = execution_id
    approval.consumed_at = timezone.now()
    approval.save(update_fields=["execution_id", "consumed_at"])
    return approval, True


@transaction.atomic
def decide_approval(approval_id, decision, binding_digest, note=""):
    lock_controller()
    try:
        approval = ActionApproval.objects.select_related("run__item").get(
            pk=approval_id
        )
    except ActionApproval.DoesNotExist:
        raise RunNotFound("Approval not found.") from None
    run = approval.run
    verify_active(run, run.item.claim_token if run.item else None)
    if decision not in {
        ActionApproval.Decision.APPROVED,
        ActionApproval.Decision.DENIED,
    }:
        raise ValidationError("Choose approve or deny.")
    if len(note) > 2000 or approval.binding_digest != binding_digest:
        raise ValidationError("The displayed action changed; reload before deciding.")
    parsed = parse_action(approval.action)
    verify_workspace(parsed, run)
    if parsed.binding_digest != binding_digest:
        raise RunConflict("Stored action changed; request a new human decision.")
    if approval.decision != ActionApproval.Decision.PENDING:
        raise RunConflict("This approval already has a decision.")
    approval.decision = decision
    approval.decision_note = note
    approval.decided_at = timezone.now()
    approval.save(update_fields=["decision", "decision_note", "decided_at"])
    return approval


def reject_secrets(action, secrets):
    encoded = json.dumps(action).lower()
    if any(secret and secret.lower() in encoded for secret in secrets):
        raise ValidationError(
            {
                "action": "Credentials and claim tokens must not be included in proposed actions."
            }
        )
