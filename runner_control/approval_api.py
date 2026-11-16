from django.core.exceptions import ValidationError
from django.db import transaction
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt

from nightly_queue.api import read_body, string_field

from .api import run_response
from .approvals import (
    approval_data,
    consume_approval,
    owned_approval,
    reject_secrets,
    request_approval,
)
from .auth import runner_api_view
from .services import owned_run, uuid_field


@runner_api_view("POST")
@transaction.atomic
def create(request, run_id):
    if len(request.body) > 32768:
        raise ValidationError({"body": "Use at most 32768 bytes."})
    try:
        data = read_body(request, ["claim_token", "operation_id", "action"])
    except RecursionError:
        raise ValidationError({"body": "Reduce JSON nesting."}) from None
    token = uuid_field(data, "claim_token")
    operation_id = uuid_field(data, "operation_id")
    action = data.get("action")
    bearer = request.headers.get("Authorization", "").split()[-1]
    reject_secrets(action, [str(token), token.hex, bearer])

    def operation():
        approval = request_approval(
            runner=request.runner_identity,
            run_id=run_id,
            claim_token=token,
            operation_id=operation_id,
            action=action,
        )
        return JsonResponse({"approval": approval_data(approval)})

    return run_response(operation)


@runner_api_view("GET")
@transaction.atomic
def listing(request, run_id):
    def operation():
        run = owned_run(request.runner_identity, run_id)
        approvals = run.approvals.order_by("-created_at")[:100]
        return JsonResponse(
            {"approvals": [approval_data(entry) for entry in approvals]}
        )

    return run_response(operation)


@runner_api_view("GET")
@transaction.atomic
def detail(request, run_id, approval_id):
    def operation():
        approval = owned_approval(request.runner_identity, run_id, approval_id)
        return JsonResponse({"approval": approval_data(approval)})

    return run_response(operation)


@runner_api_view("POST")
@transaction.atomic
def consume(request, run_id, approval_id):
    data = read_body(request, ["claim_token", "binding_digest", "execution_id"])
    token = uuid_field(data, "claim_token")
    execution_id = uuid_field(data, "execution_id")
    binding_digest = string_field(data, "binding_digest", max_length=64)

    def operation():
        approval, permit = consume_approval(
            runner=request.runner_identity,
            run_id=run_id,
            claim_token=token,
            approval_id=approval_id,
            binding_digest=binding_digest,
            execution_id=execution_id,
        )
        return JsonResponse(
            {
                "permit": permit,
                "execution_id": str(approval.execution_id),
                "approval": approval_data(approval),
            }
        )

    return run_response(operation)


@csrf_exempt
def collection(request, run_id):
    if request.method == "GET":
        return listing(request, run_id)
    return create(request, run_id)
