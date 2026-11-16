import json

from django.core.exceptions import ValidationError
from django.http import JsonResponse
from django.views.decorators.debug import sensitive_variables

from nightly_queue.api import read_body, string_field
from work_queue.api import item_data

from .auth import runner_api_view
from .models import Run
from .services import (
    MAX_EVIDENCE_BYTES,
    RunConflict,
    RunNotFound,
    claim_run,
    finish_run,
    owned_run,
    record_evidence,
    recover_claim,
    redact_payload,
    uuid_field,
)


def evidence_data(evidence):
    return {
        "id": evidence.pk,
        "operation_id": str(evidence.operation_id),
        "kind": evidence.kind,
        "data": evidence.payload,
        "created_at": evidence.created_at.isoformat(),
    }


def run_data(run):
    evidence = list(run.evidence.order_by("-pk")[:10])
    evidence_count = run.evidence.count()
    return {
        "id": str(run.pk),
        "runner_id": run.runner_id,
        "item_id": run.item_id,
        "state": run.state,
        "stage": run.stage,
        "wait_reason": run.wait_reason,
        "configuration_revision": run.configuration_revision,
        "configuration_snapshot": run.configuration_snapshot,
        "context_reference": run.context_reference,
        "plan_reference": run.plan_reference,
        "created_at": run.created_at.isoformat(),
        "updated_at": run.updated_at.isoformat(),
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "result_status": run.result_status,
        "result_url": run.result_url,
        "error_summary": run.error_summary,
        "finish_digest": run.finish_digest,
        "cancellation_requested_at": (
            run.cancellation_requested_at.isoformat()
            if run.cancellation_requested_at
            else None
        ),
        "cancellation_confirmed_at": (
            run.cancellation_confirmed_at.isoformat()
            if run.cancellation_confirmed_at
            else None
        ),
        "cancellation_reason": run.cancellation_reason,
        "evidence": [evidence_data(entry) for entry in reversed(evidence)],
        "evidence_count": evidence_count,
        "evidence_has_more": evidence_count > len(evidence),
    }


def response_data(run, *, include_claim=False):
    item = None
    if run.item is not None:
        item = item_data(run.item, include_claim=include_claim)
        item["repository_url"] = run.item.repository_url
        item["base_branch"] = run.item.base_branch
    return {"run": run_data(run), "item": item}


def run_response(operation):
    try:
        return operation()
    except RunNotFound:
        return JsonResponse({"error": "Run not found."}, status=404)
    except RunConflict as exc:
        return JsonResponse({"error": str(exc)}, status=409)


@runner_api_view("GET")
def identity(request):
    runner = request.runner_identity
    return JsonResponse(
        {
            "schema_version": 1,
            "runner": {"id": runner.pk, "name": runner.name},
            "capabilities": {
                "durable_runs": True,
                "idempotent_claims": True,
                "execution_enabled": False,
            },
        }
    )


@runner_api_view("POST")
def claim(request):
    data = read_body(request, ["run_id", "configuration_revision"])
    run_id = uuid_field(data, "run_id")
    revision = string_field(data, "configuration_revision", max_length=64)

    def operation():
        run = claim_run(
            runner=request.runner_identity,
            run_id=run_id,
            configuration_revision=revision,
        )
        include_claim = run.state == Run.State.ACTIVE
        return JsonResponse(response_data(run, include_claim=include_claim))

    return run_response(operation)


@runner_api_view("GET")
def active(request):
    run = Run.objects.filter(
        runner=request.runner_identity, state=Run.State.ACTIVE
    ).first()
    if run is None:
        return JsonResponse({"run": None, "item": None})
    return JsonResponse(response_data(run))


@runner_api_view("GET")
def detail(request, run_id):
    def operation():
        return JsonResponse(response_data(owned_run(request.runner_identity, run_id)))

    return run_response(operation)


@runner_api_view("GET")
def evidence(request, run_id):
    try:
        after = int(request.GET.get("after", "0"))
        if not 0 <= after < 2**63:
            raise ValueError
    except ValueError:
        raise ValidationError(
            {"after": "Send a nonnegative evidence cursor."}
        ) from None
    kind = request.GET.get("kind", "")
    if len(kind) > 80:
        raise ValidationError({"kind": "Use at most 80 characters."})

    def operation():
        run = owned_run(request.runner_identity, run_id)
        entries = run.evidence.filter(pk__gt=after)
        if kind:
            entries = entries.filter(kind=kind)
        page = list(entries.order_by("pk")[:11])
        has_more = len(page) > 10
        page = page[:10]
        return JsonResponse(
            {
                "evidence": [evidence_data(entry) for entry in page],
                "has_more": has_more,
                "next_after": page[-1].pk if has_more else None,
            }
        )

    return run_response(operation)


@runner_api_view("POST")
def recover(request, run_id):
    read_body(request, [])

    def operation():
        run = recover_claim(runner=request.runner_identity, run_id=run_id)
        return JsonResponse(response_data(run, include_claim=True))

    return run_response(operation)


@sensitive_variables()
def bounded_data(request, fields):
    if len(request.body) > MAX_EVIDENCE_BYTES:
        raise ValidationError({"body": f"Use at most {MAX_EVIDENCE_BYTES} bytes."})
    try:
        data = read_body(request, fields)
    except RecursionError:
        raise ValidationError({"body": "Reduce JSON nesting."}) from None
    payload = data.get("data", {})
    if not isinstance(payload, dict):
        raise ValidationError({"data": "Send a JSON object."})
    try:
        json.dumps(payload, allow_nan=False)
    except (ValueError, RecursionError):
        raise ValidationError({"data": "Send finite JSON values."}) from None
    claim = uuid_field(data, "claim_token")
    token = str(claim)
    bearer = request.headers.get("Authorization", "").split()[-1]
    data = redact_payload(
        data,
        [token, claim.hex, bearer, request.headers.get("X-NQ-Controller-Secret", "")],
    )
    data["claim_token"] = token
    return data


@runner_api_view("POST")
@sensitive_variables()
def progress(request, run_id):
    fields = [
        "operation_id",
        "claim_token",
        "stage",
        "wait_reason",
        "context_reference",
        "plan_reference",
        "data",
    ]
    data = bounded_data(request, fields)
    stage = string_field(data, "stage", max_length=20)
    if stage not in Run.Stage.values or stage == Run.Stage.FINISHED:
        raise ValidationError({"stage": "Choose an active runner stage."})
    updates = {"stage": stage}
    for field, limit in [
        ("wait_reason", 2000),
        ("context_reference", 200),
        ("plan_reference", 200),
    ]:
        if field in data:
            updates[field] = string_field(data, field, max_length=limit)
    payload = {"progress": updates, "data": data.get("data", {})}
    return write_evidence(request, run_id, data, "progress", payload, updates)


@runner_api_view("POST")
@sensitive_variables()
def artifact(request, run_id):
    data = bounded_data(request, ["operation_id", "claim_token", "kind", "data"])
    kind = string_field(data, "kind", max_length=80)
    if not kind or kind in {"progress", "plan_handoff"}:
        raise ValidationError(
            {
                "kind": "Choose a nonempty artifact kind other than progress or plan_handoff."
            }
        )
    return write_evidence(request, run_id, data, kind, data.get("data", {}))


@sensitive_variables()
def write_evidence(request, run_id, data, kind, payload, updates=None):
    operation_id = uuid_field(data, "operation_id")
    token = uuid_field(data, "claim_token")

    @sensitive_variables()
    def operation():
        entry = record_evidence(
            runner=request.runner_identity,
            run_id=run_id,
            claim_token=token,
            operation_id=operation_id,
            kind=kind,
            payload=payload,
            progress=updates,
        )
        return JsonResponse({"evidence": evidence_data(entry)})

    return run_response(operation)


@runner_api_view("POST")
@sensitive_variables()
def finish(request, run_id):
    data = bounded_data(
        request, ["claim_token", "status", "result_url", "error_summary"]
    )
    token = uuid_field(data, "claim_token")
    result = {
        "status": string_field(data, "status", max_length=9),
        "result_url": string_field(data, "result_url", max_length=500),
        "error_summary": string_field(data, "error_summary", max_length=8000),
    }

    @sensitive_variables()
    def operation():
        run = finish_run(
            runner=request.runner_identity, run_id=run_id, claim_token=token, **result
        )
        return JsonResponse(response_data(run))

    return run_response(operation)
