import json

from django.core.exceptions import ValidationError
from django.http import JsonResponse
from django.shortcuts import get_object_or_404

from nightly_queue.api import read_body, string_field

from .api import run_response
from .auth import runner_api_view
from .services import owned_run, redact_payload, uuid_field
from .plan_presentation import present_payload
from . import plan_services

MAX_CONTEXT_BYTES = 4 * 1024 * 1024
MAX_PLAN_BYTES = 256 * 1024


def document_data(entry, *, include_document=True, bearer=""):
    result = {
        "id": str(entry.pk),
        "operation_id": str(entry.operation_id),
        "digest": entry.digest,
        "reason": entry.reason,
        "created_at": entry.created_at.isoformat(),
    }
    if include_document:
        result["document"] = entry.document
    if hasattr(entry, "sequence"):
        result.update(
            sequence=entry.sequence,
            inspected_base_commit=entry.inspected_base_commit,
            resolved_base_branch=entry.resolved_base_branch,
            discussion_digest=entry.discussion_digest,
            changes=entry.changes,
        )
    else:
        result.update(
            version=entry.version,
            context_id=str(entry.context_id),
            supersedes=str(entry.supersedes_id) if entry.supersedes_id else None,
        )
    values, error = present_payload(entry.run, list(result.values()), bearer=bearer)
    if error:
        return {
            "document": None,
            "reason": error,
            "presentation_only": True,
            "unavailable": True,
        }
    safe = dict(zip(result, values))
    safe["redacted"] = safe != result
    safe["presentation_only"] = include_document and safe["document"] != entry.document
    safe["unavailable"] = False
    return safe


def request_bearer(request):
    return request.headers.get("Authorization", "").split()[-1]


def document_body(request, fields, limit):
    if len(request.body) > limit:
        raise ValidationError({"body": "Document exceeds size limit."})
    try:
        data = read_body(request, fields)
        try:
            json.dumps(data, allow_nan=False)
        except ValueError:
            raise ValidationError({"body": "Send finite JSON values."}) from None
        token = uuid_field(data, "claim_token")
        bearer = request.headers.get("Authorization", "").split()[-1]
        safe = redact_payload(
            data,
            [
                str(token),
                token.hex,
                bearer,
                request.headers.get("X-NQ-Controller-Secret", ""),
            ],
        )
        if "document" in data and safe["document"] != data["document"]:
            raise ValidationError(
                {
                    "document": "Saved documents must not contain credentials or secret fields."
                }
            )
    except RecursionError:
        raise ValidationError({"body": "Reduce JSON nesting."}) from None
    safe["claim_token"] = token
    safe["operation_id"] = uuid_field(data, "operation_id")
    return safe


def document_page(request, run, kind):
    try:
        after = int(request.GET.get("after", "0"))
        if not 0 <= after < 2**31:
            raise ValueError
    except ValueError:
        raise ValidationError(
            {"after": "Send a nonnegative document cursor."}
        ) from None
    field = "sequence" if kind == "contexts" else "version"
    entries = list(
        getattr(run, kind).filter(**{field + "__gt": after}).order_by(field)[:11]
    )
    more = len(entries) > 10
    entries = entries[:10]
    return JsonResponse(
        {
            kind: [
                document_data(
                    entry, include_document=False, bearer=request_bearer(request)
                )
                for entry in entries
            ],
            "has_more": more,
            "next_after": getattr(entries[-1], field) if more else None,
        }
    )


@runner_api_view("GET")
def context_detail(request, run_id, document_id):
    def operation():
        run = owned_run(request.runner_identity, run_id)
        return JsonResponse(
            {
                "context": document_data(
                    get_object_or_404(run.contexts, pk=document_id),
                    bearer=request_bearer(request),
                )
            }
        )

    return run_response(operation)


@runner_api_view("GET")
def plan_detail(request, run_id, document_id):
    def operation():
        run = owned_run(request.runner_identity, run_id)
        return JsonResponse(
            {
                "plan": document_data(
                    get_object_or_404(run.plans, pk=document_id),
                    bearer=request_bearer(request),
                )
            }
        )

    return run_response(operation)


@runner_api_view("GET", "POST")
def contexts(request, run_id):
    if request.method == "GET":
        return run_response(
            lambda: document_page(
                request, owned_run(request.runner_identity, run_id), "contexts"
            )
        )
    data = document_body(
        request,
        [
            "claim_token",
            "operation_id",
            "document",
            "base_commit",
            "resolved_base_branch",
            "reason",
        ],
        MAX_CONTEXT_BYTES,
    )
    kwargs = {
        "runner": request.runner_identity,
        "run_id": run_id,
        "claim_token": data["claim_token"],
        "operation_id": data["operation_id"],
        "document": data.get("document"),
        "base_commit": string_field(data, "base_commit", max_length=64),
        "resolved_base_branch": string_field(
            data, "resolved_base_branch", max_length=200
        ),
        "reason": string_field(data, "reason", max_length=2000),
    }
    return run_response(
        lambda: JsonResponse(
            {
                "context": document_data(
                    plan_services.save_context(**kwargs), bearer=request_bearer(request)
                )
            }
        )
    )


@runner_api_view("GET", "POST")
def plans(request, run_id):
    if request.method == "GET":
        return run_response(
            lambda: document_page(
                request, owned_run(request.runner_identity, run_id), "plans"
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
        lambda: JsonResponse(
            {
                "plan": document_data(
                    plan_services.save_plan(**kwargs), bearer=request_bearer(request)
                )
            }
        )
    )


def handoff_data(plan, context, *, bearer=""):
    return {
        "plan": document_data(plan, bearer=bearer),
        "context": document_data(context, bearer=bearer),
        "actions_require_approval": True,
    }


@runner_api_view("GET")
def handoff_detail(request, run_id, operation_id):
    def operation():
        run = owned_run(request.runner_identity, run_id)
        plan, context = plan_services.saved_handoff(run, operation_id)
        return JsonResponse(handoff_data(plan, context, bearer=request_bearer(request)))

    return run_response(operation)


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
        return JsonResponse(handoff_data(plan, context, bearer=request_bearer(request)))

    return run_response(operation)
