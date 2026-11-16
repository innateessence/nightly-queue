from django.core.exceptions import ValidationError
from django.http import JsonResponse

from nightly_queue.api import read_body, string_field

from .api import run_response
from .auth import runner_api_view
from .services import owned_run, redact_payload, uuid_field
from . import plan_services

MAX_CONTEXT_BYTES = 4 * 1024 * 1024
MAX_PLAN_BYTES = 256 * 1024


def document_data(entry):
    result = {
        "id": str(entry.pk),
        "operation_id": str(entry.operation_id),
        "document": entry.document,
        "digest": entry.digest,
        "reason": entry.reason,
        "created_at": entry.created_at.isoformat(),
    }
    if hasattr(entry, "sequence"):
        result.update(
            sequence=entry.sequence,
            inspected_base_commit=entry.inspected_base_commit,
            discussion_digest=entry.discussion_digest,
            changes=entry.changes,
        )
    else:
        result.update(
            version=entry.version,
            context_id=str(entry.context_id),
            supersedes=str(entry.supersedes_id) if entry.supersedes_id else None,
        )
    return result


def document_body(request, fields, limit):
    if len(request.body) > limit:
        raise ValidationError({"body": "Document exceeds size limit."})
    try:
        data = read_body(request, fields)
        token = uuid_field(data, "claim_token")
        bearer = request.headers.get("Authorization", "").split()[-1]
        safe = redact_payload(data, [str(token), token.hex, bearer])
    except RecursionError:
        raise ValidationError({"body": "Reduce JSON nesting."}) from None
    safe["claim_token"] = token
    safe["operation_id"] = uuid_field(data, "operation_id")
    return safe


@runner_api_view("GET", "POST")
def contexts(request, run_id):
    if request.method == "GET":
        return run_response(
            lambda: JsonResponse(
                {
                    "contexts": [
                        document_data(entry)
                        for entry in owned_run(
                            request.runner_identity, run_id
                        ).contexts.all()
                    ]
                }
            )
        )
    data = document_body(
        request,
        ["claim_token", "operation_id", "document", "base_commit", "reason"],
        MAX_CONTEXT_BYTES,
    )
    kwargs = {
        "runner": request.runner_identity,
        "run_id": run_id,
        "claim_token": data["claim_token"],
        "operation_id": data["operation_id"],
        "document": data.get("document"),
        "base_commit": string_field(data, "base_commit", max_length=64),
        "reason": string_field(data, "reason", max_length=2000),
    }
    return run_response(
        lambda: JsonResponse(
            {"context": document_data(plan_services.save_context(**kwargs))}
        )
    )


@runner_api_view("GET", "POST")
def plans(request, run_id):
    if request.method == "GET":
        return run_response(
            lambda: JsonResponse(
                {
                    "plans": [
                        document_data(entry)
                        for entry in owned_run(
                            request.runner_identity, run_id
                        ).plans.all()
                    ]
                }
            )
        )
    data = document_body(
        request, ["claim_token", "operation_id", "document", "reason"], MAX_PLAN_BYTES
    )
    kwargs = {
        "runner": request.runner_identity,
        "run_id": run_id,
        "claim_token": data["claim_token"],
        "operation_id": data["operation_id"],
        "document": data.get("document"),
        "reason": string_field(data, "reason", max_length=2000),
    }
    return run_response(
        lambda: JsonResponse({"plan": document_data(plan_services.save_plan(**kwargs))})
    )


@runner_api_view("POST")
def handoff(request, run_id):
    data = document_body(
        request,
        ["claim_token", "operation_id", "context_id", "base_commit"],
        MAX_PLAN_BYTES,
    )
    kwargs = {
        "runner": request.runner_identity,
        "run_id": run_id,
        "claim_token": data["claim_token"],
        "operation_id": data["operation_id"],
        "context_id": uuid_field(data, "context_id"),
        "base_commit": string_field(data, "base_commit", max_length=64),
    }

    def operation():
        plan, context = plan_services.handoff(**kwargs)
        return JsonResponse(
            {
                "plan": document_data(plan),
                "context": document_data(context),
                "actions_require_approval": True,
            }
        )

    return run_response(operation)
