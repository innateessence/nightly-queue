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


@require_GET
def history(request):
    runs = Run.objects.select_related("item", "runner").order_by("-created_at", "-pk")
    status = request.GET.get("state", "")
    if status in Run.State.values:
        runs = runs.filter(state=status)
    page = Paginator(runs, 25).get_page(request.GET.get("page"))
    page.object_list = [present_run(run) for run in page]
    configuration = configuration_document()
    reports = present_gates(
        RunnerGateEvidence.objects.select_related("runner"), configuration
    )
    health = [
        present_global_record(entry, configuration, run=report_run(entry))
        for entry in RunnerHealth.objects.select_related("runner")
    ]
    response = render(
        request,
        "runner_control/plan_history.html",
        {
            "runs": page,
            "page": page,
            "state_filter": status,
            "health_reports": health,
            "configuration": present_configuration(configuration),
            "runner_gate_evidence": reports,
        },
    )
    response["Cache-Control"] = "no-store"
    return response


@require_GET
def detail(request, run_id):
    run = get_object_or_404(Run.objects.select_related("item"), pk=run_id)
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
            "run": present_run(run),
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
