from django.http import JsonResponse

from nightly_queue.api import read_body, string_field

from .api import run_response
from .auth import runner_api_view
from .retention import acknowledge, plan, purge_candidates, purge_run, workspace_removed
from .services import uuid_field
from .plan_presentation import present_configuration_payload, qualification_runs


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


@runner_api_view("GET", "POST")
def release_suite(request, qualification_id):
    from django.shortcuts import get_object_or_404
    from .configuration import configuration_document
    from .models import ReleaseQualification
    from .release import authorized, complete

    entry = get_object_or_404(
        ReleaseQualification, pk=qualification_id, runner=request.runner_identity
    )
    if request.method == "POST":
        if len(request.body) > 65536:
            from django.core.exceptions import ValidationError

            raise ValidationError("Release evidence is limited to 64 KiB.")
        data = read_body(request, ["report"])
        return run_response(
            lambda: _qualified(
                complete(entry.pk, request.runner_identity, data.get("report", {}))
            )
        )

    def response():
        configuration = configuration_document()
        authorized(entry, request.runner_identity, configuration)
        secrets = [
            request.headers.get("Authorization", "").split()[-1],
            request.headers.get("X-NQ-Controller-Secret", ""),
        ]
        evidence, error = present_configuration_payload(
            configuration,
            entry.evidence,
            runs=qualification_runs(entry),
            secrets=secrets,
            preserve_secret_versions=True,
        )
        if error:
            from django.core.exceptions import ValidationError

            raise ValidationError(error)
        return JsonResponse(
            {
                "schema_version": 1,
                "qualification": {
                    "id": str(entry.pk),
                    "repository_url": entry.repository_url,
                    "environment_digest": entry.environment_digest,
                    "code_revision": entry.code_revision,
                    "scope_digest": entry.scope_digest,
                    "expires_at": entry.expires_at.isoformat(),
                    "qualified_at": (
                        entry.qualified_at.isoformat() if entry.qualified_at else None
                    ),
                    "evidence": evidence,
                    "evidence_presentation_only": evidence != entry.evidence,
                },
            }
        )

    return run_response(response)


def _qualified(entry):
    return JsonResponse(
        {
            "qualified_at": entry.qualified_at.isoformat(),
            "expires_at": entry.expires_at.isoformat(),
        }
    )


@runner_api_view("POST")
def release_claim(request, qualification_id):
    from django.core.exceptions import ValidationError
    from django.db import transaction
    from django.shortcuts import get_object_or_404
    from work_queue.services import claimable_items
    from .api import response_data
    from .configuration import configuration_document
    from .models import ReleaseQualification
    from .release import authorized
    from .services import RunConflict, claim_run, lock_controller

    data = read_body(request, ["run_id", "item_id"])
    run_id = uuid_field(data, "run_id")
    item_id = data.get("item_id")
    if type(item_id) is not int:
        raise ValidationError({"item_id": "Use the designated queued item ID."})

    def response():
        with transaction.atomic():
            lock_controller(request.runner_identity)
            entry = get_object_or_404(
                ReleaseQualification,
                pk=qualification_id,
                runner=request.runner_identity,
            )
            configuration = configuration_document()
            authorized(entry, request.runner_identity, configuration)
            from .models import Run

            previous = Run.objects.filter(
                pk=run_id, runner=request.runner_identity, item_id=item_id
            ).first()
            if previous is not None:
                if (
                    previous.created_at < entry.created_at
                    or previous.configuration_snapshot.get("repository_url")
                    != entry.repository_url
                ):
                    raise RunConflict(
                        "Controlled claim replay is outside authorized scope."
                    )
                return JsonResponse(
                    response_data(
                        previous, include_claim=previous.state == Run.State.ACTIVE
                    )
                )
            candidate = claimable_items().select_related("project").first()
            if (
                candidate is None
                or candidate.pk != item_id
                or candidate.repository_url != entry.repository_url
            ):
                raise RunConflict(
                    "The next claim must be the explicitly designated test item in the authorized repository."
                )
            run = claim_run(
                runner=request.runner_identity,
                run_id=run_id,
                configuration_revision=configuration["revision"],
            )
            return JsonResponse(response_data(run, include_claim=True))

    return run_response(response)


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
        request.headers.get("X-NQ-Controller-Secret", ""),
    ]
    from .models import Run

    active = Run.objects.filter(
        runner=request.runner_identity, state=Run.State.ACTIVE
    ).first()
    safe, error = present_configuration_payload(
        configuration_document(), data, run=active, secrets=secrets
    )
    if error:
        raise ValidationError(error)
    if active is not None:
        safe["run_id"] = str(active.pk)
    RunnerHealth.objects.update_or_create(
        runner=request.runner_identity, defaults={"report": safe}
    )
    return JsonResponse({"received": True})
