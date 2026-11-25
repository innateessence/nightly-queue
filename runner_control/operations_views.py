from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.views.decorators.http import require_POST

from .services import RunConflict, RunNotFound


@require_POST
def cancel(request, run_id):
    from .recovery_services import request_cancellation

    try:
        request_cancellation(
            run_id, request.POST.get("reason", "Operator requested cancellation")
        )
    except RunNotFound:
        raise Http404 from None
    except RunConflict as exc:
        return HttpResponse(str(exc), status=409, content_type="text/plain")
    return redirect("runner_plan_detail", run_id=run_id)


@require_POST
def retry(request, run_id):
    from django.contrib import messages
    from work_queue.models import QueueItem
    from work_queue.services import requeue_items
    from .models import Run

    run = get_object_or_404(Run, pk=run_id)
    if run.item_id and (run.item.deleted_at or run.item.project.deleted_at):
        return HttpResponse(
            "This work item or project was deleted and cannot be retried.",
            status=409,
            content_type="text/plain",
        )
    try:
        result = requeue_items(
            QueueItem.objects.filter(pk=run.item_id),
            return_summary=True,
            expected_run_id=run.pk,
        )
    except RunConflict as exc:
        return HttpResponse(str(exc), status=409, content_type="text/plain")
    if result["pending"]:
        messages.info(
            request,
            "Retry requested; the current native attempt keeps ownership until termination is confirmed.",
        )
    elif result["held"]:
        messages.warning(request, "Existing delivery must be reconciled before retry.")
    elif result["requeued"]:
        messages.success(request, "Work queued for a new attempt.")
    else:
        messages.info(request, "This work is not retryable.")
    return redirect("runner_plan_detail", run_id=run_id)
