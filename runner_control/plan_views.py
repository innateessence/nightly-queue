from django.shortcuts import get_object_or_404, render
from django.views.decorators.http import require_GET

from .models import Run
from .plan_services import valid_handoff
from .services import RunConflict


@require_GET
def history(request):
    runs = Run.objects.select_related("item").order_by("-created_at")[:100]
    return render(request, "runner_control/plan_history.html", {"runs": runs})


@require_GET
def detail(request, run_id):
    run = get_object_or_404(Run.objects.select_related("item"), pk=run_id)
    reason = ""
    try:
        valid_handoff(run)
    except RunConflict as exc:
        reason = str(exc)
    return render(
        request,
        "runner_control/plan_detail.html",
        {
            "run": run,
            "contexts": run.contexts.all(),
            "plans": run.plans.all(),
            "handoff_reason": reason,
        },
    )
