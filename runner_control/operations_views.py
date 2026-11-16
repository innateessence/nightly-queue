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


@require_POST
def authorize_release(request):
    from django.core.exceptions import ValidationError
    from .models import RunnerIdentity
    from .release import authorize

    runner = get_object_or_404(
        RunnerIdentity, pk=request.POST.get("runner_id"), enabled=True
    )
    if request.POST.get("authorize_live") != "yes":
        return HttpResponse(
            "Explicit live test and backend-call authorization is required.", status=400
        )
    try:
        authorize(
            runner,
            request.POST.get("repository_url", ""),
            request.POST.get("environment_digest", ""),
            request.POST.get("code_revision", ""),
            request.POST.get("note", ""),
        )
    except (ValidationError, RunConflict) as exc:
        return HttpResponse(str(exc), status=400, content_type="text/plain")
    return redirect("api_settings")


@require_POST
def revoke_release(request, qualification_id):
    from django.utils import timezone
    from .models import ReleaseQualification

    entry = get_object_or_404(ReleaseQualification, pk=qualification_id)
    entry.revoked_at = timezone.now()
    entry.save(update_fields=["revoked_at"])
    return redirect("api_settings")
