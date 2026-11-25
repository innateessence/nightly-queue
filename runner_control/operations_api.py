from django.http import JsonResponse

from vibearr_app.api import read_body, string_field

from .api import run_response
from .auth import runner_api_view
from .retention import acknowledge, plan, purge_candidates, purge_run, workspace_removed
from .services import uuid_field
from .plan_presentation import present_configuration_payload


@runner_api_view("GET", "POST")
def retention(request):
    if request.method == "POST":
        read_body(request, [])
        for run in purge_candidates(request.runner_identity):
            run_response(lambda run=run: purge_run(run.pk))
    return JsonResponse(plan(request.runner_identity))


@runner_api_view("POST")
def retention_ack(request, run_id):
    data = read_body(request, ["claim_token", "finish_digest"])
    token = uuid_field(data, "claim_token")
    digest = string_field(data, "finish_digest", max_length=64)
    return run_response(lambda: _ack(request.runner_identity, run_id, token, digest))


def _ack(runner, run_id, token, digest):
    entry = acknowledge(runner, run_id, token, digest)
    return JsonResponse(
        {"acknowledged_at": entry.reporting_acknowledged_at.isoformat()}
    )


@runner_api_view("POST")
def retention_workspace(request, run_id):
    data = read_body(request, ["status", "resource"])
    status = string_field(data, "status", max_length=20)
    return run_response(
        lambda: _removed(
            request.runner_identity, run_id, status, data.get("resource", "workspace")
        )
    )


def _removed(runner, run_id, status, resource):
    workspace_removed(runner, run_id, status, resource)
    return JsonResponse({"removed": True})


@runner_api_view("POST")
def health(request):
    from django.core.exceptions import ValidationError
    from .models import RunnerHealth

    data = read_body(
        request,
        [
            "free_bytes",
            "minimum_free_bytes",
            "removed",
            "missing",
            "busy",
            "blocking_reason",
        ],
    )
    for key in ("free_bytes", "minimum_free_bytes", "removed", "missing", "busy"):
        if type(data.get(key)) is not int or not 0 <= data[key] < 2**63:
            raise ValidationError({key: "Send a nonnegative integer."})
    data["blocking_reason"] = string_field(data, "blocking_reason", max_length=500)
    from .configuration import configuration_document

    secrets = [
        request.headers.get("Authorization", "").split()[-1],
        request.headers.get("X-Vibearr-Controller-Secret", ""),
    ]
    from .models import Run

    active = Run.objects.filter(
        runner=request.runner_identity, state=Run.State.ACTIVE
    ).first()
    safe, error = present_configuration_payload(
        configuration_document(request.runner_identity),
        data,
        run=active,
        secrets=secrets,
    )
    if error:
        raise ValidationError(error)
    if active is not None:
        safe["run_id"] = str(active.pk)
    RunnerHealth.objects.update_or_create(
        runner=request.runner_identity, defaults={"report": safe}
    )
    return JsonResponse({"received": True})
