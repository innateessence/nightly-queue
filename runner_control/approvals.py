from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from django.views.decorators.debug import sensitive_variables

from vibearr_runner.approvals import ActionSpec
from credentials.redaction import redact_run_payload

from .models import ActionApproval, Run
from .services import (
    RunConflict,
    RunNotFound,
    lock_controller,
    owned_run,
    redact_payload,
    verify_active,
)


def reject_native_approval(run):
    if run.configuration_snapshot.get("execution_kind") == "native":
        raise RunConflict(
            "Native execution uses project policy; human approval actions are disabled."
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


@sensitive_variables()
def approval_data(approval):
    data = {
        "id": str(approval.pk),
        "run_id": str(approval.run_id),
        "run_state": approval.run.state,
        "execution_kind": approval.run.configuration_snapshot.get(
            "execution_kind", "legacy"
        ),
        "cancellation_requested": bool(approval.run.cancellation_requested_at),
        "item_title": approval.run.item.title if approval.run.item else "",
        "operation_id": str(approval.operation_id),
        "action": approval.action,
        "binding_digest": approval.binding_digest,
        "decision": approval.decision,
        "decision_display": approval.get_decision_display(),
        "decision_note": approval.decision_note,
        "decided_at": approval.decided_at.isoformat() if approval.decided_at else None,
        "created_at": approval.created_at.isoformat(),
        "consumed_at": (
            approval.consumed_at.isoformat() if approval.consumed_at else None
        ),
        "execution_id": str(approval.execution_id) if approval.execution_id else None,
    }
    safe = redact_run_payload(approval.run, data)
    safe["content_withheld"] = safe != data
    return safe


@sensitive_variables()
def reject_selected_secrets(run, action):
    if redact_run_payload(run, action) != action:
        raise ValidationError(
            {"action": "Selected credentials must not be included in proposed actions."}
        )


@transaction.atomic
@sensitive_variables()
def request_approval(*, runner, run_id, claim_token, operation_id, action):
    lock_controller(runner)
    run = owned_run(runner, run_id)
    verify_active(run, claim_token)
    reject_native_approval(run)
    parsed = parse_action(action)
    verify_workspace(parsed, run)
    reject_selected_secrets(run, parsed.to_data())
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
@sensitive_variables()
def consume_approval(
    *, runner, run_id, claim_token, approval_id, binding_digest, execution_id
):
    lock_controller(runner)
    run = owned_run(runner, run_id)
    verify_active(run, claim_token)
    reject_native_approval(run)
    approval = owned_approval(runner, run_id, approval_id)
    if approval.binding_digest != binding_digest:
        raise RunConflict("Approval binding changed; request a new human decision.")
    parsed = parse_action(approval.action)
    verify_workspace(parsed, run)
    reject_selected_secrets(run, parsed.to_data())
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
@sensitive_variables()
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
    reject_native_approval(run)
    if decision not in {
        ActionApproval.Decision.APPROVED,
        ActionApproval.Decision.DENIED,
    }:
        raise ValidationError("Choose approve or deny.")
    if len(note) > 2000 or approval.binding_digest != binding_digest:
        raise ValidationError("The displayed action changed; reload before deciding.")
    parsed = parse_action(approval.action)
    verify_workspace(parsed, run)
    reject_selected_secrets(run, parsed.to_data())
    if parsed.binding_digest != binding_digest:
        raise RunConflict("Stored action changed; request a new human decision.")
    if approval.decision != ActionApproval.Decision.PENDING:
        raise RunConflict("This approval already has a decision.")
    approval.decision = decision
    approval.decision_note = redact_run_payload(run, note)
    approval.decided_at = timezone.now()
    approval.save(update_fields=["decision", "decision_note", "decided_at"])
    return approval


@sensitive_variables()
def reject_secrets(action, secrets):
    if redact_payload(action, secrets) != action:
        raise ValidationError(
            {
                "action": "Credentials and claim tokens must not be included in proposed actions."
            }
        )
