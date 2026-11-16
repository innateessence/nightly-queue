import json

from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.views.decorators.cache import never_cache
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.shortcuts import get_object_or_404, render
from django.views.decorators.http import require_GET

from .models import Run, RunRetention, RunnerHealth
from .configuration import configuration_document
from .gate_models import RunnerGateEvidence
from .plan_api import document_data
from .plan_presentation import present_run
from .plan_services import valid_handoff
from .services import RunConflict


@never_cache
@require_GET
def history(request):
    runs = Run.objects.select_related("item", "runner").order_by("-created_at", "-pk")
    status = request.GET.get("state", "")
    if status in Run.State.values:
        runs = runs.filter(state=status)
    page = Paginator(runs, 25).get_page(request.GET.get("page"))
    page.object_list = [present_run(run) for run in page]
    configuration = configuration_document()
    reports = list(RunnerGateEvidence.objects.select_related("runner"))
    for evidence in reports:
        next_check = parse_datetime(evidence.report["next_check_at"])
        evidence.stale = next_check is None or next_check <= timezone.now()
        evidence.configuration_changed = (
            evidence.report["configuration_revision"] != configuration["revision"]
        )
    response = render(
        request,
        "runner_control/plan_history.html",
        {
            "runs": page,
            "page": page,
            "state_filter": status,
            "health_reports": RunnerHealth.objects.select_related("runner"),
            "configuration": configuration,
            "runner_gate_evidence": reports,
        },
    )
    response["Cache-Control"] = "no-store"
    return response


@never_cache
@require_GET
def detail(request, run_id):
    run = get_object_or_404(Run.objects.select_related("item"), pk=run_id)
    reason = ""
    try:
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
    response = render(
        request,
        "runner_control/plan_detail.html",
        {
            "run": present_run(run),
            "contexts": contexts,
            "plans": plans,
            "handoff_reason": reason,
            "retention": RunRetention.objects.filter(run=run).first(),
            "evidence_page": Paginator(run.evidence.order_by("pk"), 25).get_page(
                request.GET.get("page")
            ),
            "approvals": run.approvals.all(),
            "configuration_json": json.dumps(run.configuration_snapshot, indent=2),
            "delivery": run.evidence.filter(kind__in=["delivery", "verified_delivery"])
            .order_by("-pk")
            .first(),
        },
    )
    response["Cache-Control"] = "no-store"
    return response
