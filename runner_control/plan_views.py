from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.shortcuts import get_object_or_404, render
from django.views.decorators.http import require_GET

from .models import Run
from .plan_api import document_data
from .plan_presentation import present_run
from .plan_services import valid_handoff
from .services import RunConflict


@require_GET
def history(request):
    runs = Run.objects.select_related("item").order_by("-created_at", "-pk")
    page = Paginator(runs, 25).get_page(request.GET.get("page"))
    page.object_list = [present_run(run) for run in page]
    response = render(request, "runner_control/plan_history.html", {"runs": page})
    response["Cache-Control"] = "no-store"
    return response


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
        },
    )
    response["Cache-Control"] = "no-store"
    return response
