from django.core.exceptions import ValidationError
from django.db import transaction
from django.http import JsonResponse
from django.utils import timezone
from django.utils.crypto import constant_time_compare

from vibearr_app.api import read_body, string_field

from .api import response_data, run_response
from .auth import runner_api_view, token_digest
from .models import Run, RunnerIdentity
from .services import RunConflict, lock_controller, owned_run, uuid_field, verify_active


@runner_api_view("POST")
def bind_controller(request):
    read_body(request, [])
    proof = request.headers.get("X-NQ-Controller-Secret", "")
    if len(proof) != 64 or any(
        character not in "0123456789abcdef" for character in proof
    ):
        raise ValidationError({"controller": "Use the durable private volume proof."})

    def operation():
        with transaction.atomic():
            lock_controller(request.runner_identity)
            runner = RunnerIdentity.objects.select_for_update().get(
                pk=request.runner_identity.pk
            )
            digest = token_digest(proof)
            if runner.controller_digest and not constant_time_compare(
                runner.controller_digest, digest
            ):
                raise RunConflict(
                    "Another controller volume owns this identity; operator reconciliation is required."
                )
            if not runner.controller_digest:
                if Run.objects.filter(runner=runner, state=Run.State.ACTIVE).exists():
                    raise RunConflict(
                        "Owned work predates local proof; stop the old execution and reconcile manually."
                    )
                runner.controller_digest = digest
                runner.save(update_fields=["controller_digest"])
        return JsonResponse({"controller": "bound", "schema_version": 1})

    return run_response(operation)


@runner_api_view("POST")
def cancellation(request, run_id):
    data = read_body(request, ["claim_token", "reason", "termination_confirmed"])
    token = uuid_field(data, "claim_token")
    reason = string_field(data, "reason", max_length=2000)
    confirmed = data.get("termination_confirmed", False)
    if type(confirmed) is not bool:
        raise ValidationError({"termination_confirmed": "Send a boolean."})

    def operation():
        with transaction.atomic():
            lock_controller(request.runner_identity)
            run = owned_run(request.runner_identity, run_id)
            verify_active(run, token, allow_cancellation=True)
            if run.cancellation_requested_at is None:
                if confirmed:
                    raise RunConflict(
                        "Record cancellation before confirming termination."
                    )
                run.cancellation_requested_at = timezone.now()
                run.cancellation_reason = reason or "Cancellation requested"
            if confirmed:
                from .native_services import require_termination

                require_termination(run)
                run.cancellation_confirmed_at = (
                    run.cancellation_confirmed_at or timezone.now()
                )
            run.save(
                update_fields=[
                    "cancellation_requested_at",
                    "cancellation_confirmed_at",
                    "cancellation_reason",
                    "updated_at",
                ]
            )
        return JsonResponse(response_data(run))

    return run_response(operation)


@runner_api_view("GET")
def operation_detail(request, run_id, operation_id):
    from .api import evidence_data
    from .approvals import approval_data
    from .plan_api import document_data, request_bearer

    def operation():
        run = owned_run(request.runner_identity, run_id)
        context = run.contexts.filter(operation_id=operation_id).first()
        if context:
            return JsonResponse(
                {
                    "response": {
                        "context": document_data(
                            context, bearer=request_bearer(request)
                        )
                    }
                }
            )
        plan = run.plans.filter(operation_id=operation_id).first()
        if plan:
            return JsonResponse(
                {
                    "response": {
                        "plan": document_data(plan, bearer=request_bearer(request))
                    }
                }
            )
        approval = run.approvals.filter(operation_id=operation_id).first()
        if approval:
            return JsonResponse({"response": {"approval": approval_data(approval)}})
        evidence = run.evidence.filter(operation_id=operation_id).first()
        if evidence is None:
            return JsonResponse({"error": "Operation not recorded."}, status=404)
        if evidence.kind == "plan_handoff":
            plan = run.plans.get(pk=evidence.payload["plan_id"])
            context = run.contexts.get(pk=evidence.payload["context_id"])
            response = {
                "plan": document_data(plan, bearer=request_bearer(request)),
                "context": document_data(context, bearer=request_bearer(request)),
                "actions_require_approval": True,
            }
        else:
            response = {"evidence": evidence_data(evidence)}
        return JsonResponse({"response": response})

    return run_response(operation)
