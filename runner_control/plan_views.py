import json

from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.shortcuts import get_object_or_404, render
from django.views.decorators.http import require_GET

from .models import Run, RunRetention, RunnerHealth
from .configuration import configuration_document
from .gate_models import RunnerGateEvidence
from .plan_api import document_data
from .plan_presentation import (
    present_run,
    report_run,
    present_payload,
    present_approval,
    present_evidence,
    present_global_record,
    present_gates,
    present_configuration,
)
from .plan_services import valid_handoff
from .services import RunConflict
from .factory_presentation import (
    ARTIFACT_PAGE_SIZE,
    artifact_data,
    prepare_factory_runs,
    present_artifact,
)


@require_GET
def activity(request):
    runs = Run.objects.select_related("item", "runner").filter(state=Run.State.ACTIVE)
    page = Paginator(runs, 25).get_page(request.GET.get("page"))
    page.object_list = [present_run(run) for run in prepare_factory_runs(page)]
    response = render(
        request,
        "runner_control/activity.html",
        {"runs": page.object_list, "page": page},
    )
    response["Cache-Control"] = "no-store"
    return response


@require_GET
def history(request):
    runs = Run.objects.select_related("item", "runner").order_by("-created_at", "-pk")
    status = request.GET.get("state", "")
    if status not in Run.State.values:
        status = ""
    if status in Run.State.values:
        runs = runs.filter(state=status)
    page = Paginator(runs, 25).get_page(request.GET.get("page"))
    page.object_list = [present_run(run) for run in prepare_factory_runs(page)]
    response = render(
        request,
        "runner_control/plan_history.html",
        {
            "runs": page,
            "page": page,
            "state_filter": status,
            "states": Run.State.choices,
        },
    )
    response["Cache-Control"] = "no-store"
    return response


@require_GET
def system(request):
    from credentials.models import APISettings, Credential

    configuration = configuration_document()
    reports = present_gates(RunnerGateEvidence.objects.select_related("runner"))
    health = [
        present_global_record(
            entry, configuration_document(entry.runner), run=report_run(entry)
        )
        for entry in RunnerHealth.objects.select_related("runner")
    ]
    response = render(
        request,
        "runner_control/system.html",
        {
            "configuration": present_configuration(configuration),
            "runner_gate_evidence": reports,
            "health_reports": health,
            "connection_count": Credential.objects.filter(
                revoked_at__isnull=True
            ).count(),
            "sync": APISettings.objects.filter(pk=1).first(),
        },
    )
    response["Cache-Control"] = "no-store"
    return response


@require_GET
def detail(request, run_id):
    run = get_object_or_404(Run.objects.select_related("item", "runner"), pk=run_id)
    safe_run = present_run(prepare_factory_runs([run])[0])
    factory = Paginator(
        run.factory_artifacts.order_by("-sequence"), ARTIFACT_PAGE_SIZE
    ).get_page(request.GET.get("factory_page"))
    documents, factory_error = present_payload(
        run, [artifact_data(entry) for entry in factory]
    )
    factory.object_list = (
        [
            present_artifact(
                entry,
                safe_run.factory.get("binding"),
                specification_id=safe_run.factory.get("specification_artifact_id", ""),
                intake_id=safe_run.factory.get("intake_artifact_id", ""),
            )
            for entry in documents
        ]
        if documents is not None
        else []
    )
    native_run = run.configuration_snapshot.get("execution_kind") == "native"
    reason = ""
    try:
        if not native_run:
            valid_handoff(run)
    except RunConflict as exc:
        reason = str(exc)
    except ValidationError:
        reason = "Saved plan handoff is blocked by credential redaction."
    contexts = Paginator(run.contexts.order_by("-sequence"), 1).get_page(
        request.GET.get("context_page")
    )
    plans = Paginator(run.plans.order_by("-version"), 1).get_page(
        request.GET.get("plan_page")
    )
    contexts.object_list = [document_data(entry) for entry in contexts]
    plans.object_list = [document_data(entry) for entry in plans]
    evidence = Paginator(run.evidence.order_by("pk"), 25).get_page(
        request.GET.get("page")
    )
    evidence.object_list = [present_evidence(entry) for entry in evidence]
    configuration, error = present_payload(
        run, run.configuration_snapshot, preserve_secret_versions=True
    )
    delivery = (
        run.evidence.filter(kind__in=["delivery", "verified_delivery"])
        .order_by("-pk")
        .first()
    )
    reason, reason_error = present_payload(run, reason)
    response = render(
        request,
        "runner_control/plan_detail.html",
        {
            "run": safe_run,
            "factory_artifacts": factory,
            "factory_error": factory_error,
            "native_run": native_run,
            "contexts": contexts,
            "plans": plans,
            "handoff_reason": reason if reason is not None else reason_error,
            "retention": RunRetention.objects.filter(run=run).first(),
            "evidence_page": evidence,
            "approvals": [
                present_approval(approval) for approval in run.approvals.all()
            ],
            "configuration_json": (
                json.dumps(configuration, indent=2)
                if configuration is not None
                else error
            ),
            "delivery": present_evidence(delivery) if delivery is not None else None,
        },
    )
    response["Cache-Control"] = "no-store"
    return response
