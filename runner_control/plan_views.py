from django.core.paginator import Paginator
from django.shortcuts import get_object_or_404, render
from django.views.decorators.http import require_GET

from .models import Run
from .plan_services import valid_handoff
from .services import RunConflict


@require_GET
def history(request):
    runs = Run.objects.select_related("item").order_by("-created_at", "-pk")
    page = Paginator(runs, 25).get_page(request.GET.get("page"))
    return render(request, "runner_control/plan_history.html", {"runs": page})


@require_GET
def detail(request, run_id):
    run = get_object_or_404(Run.objects.select_related("item"), pk=run_id)
    reason = ""
    try:
        valid_handoff(run)
    except RunConflict as exc:
        reason = str(exc)
    contexts = Paginator(run.contexts.order_by("-sequence"), 1).get_page(
        request.GET.get("context_page")
    )
    plans = Paginator(run.plans.order_by("-version"), 1).get_page(
        request.GET.get("plan_page")
    )
    return render(
        request,
        "runner_control/plan_detail.html",
        {
            "run": run,
            "contexts": contexts,
            "plans": plans,
            "handoff_reason": reason,
        },
    )
