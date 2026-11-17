from django.contrib import messages
from django.db import transaction
from django.db.models.deletion import ProtectedError
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from .configuration import configuration_document, settings_values
from .configuration_views import window_labels
from .plan_presentation import form_presentation_error, present_configuration_payload
from .release import observe_settings
from .services import lock_controller
from .settings_forms import RunnerScheduleForm
from .settings_models import RunnerSchedule, RunnerSettings, ensure_default_schedule


def presentation_configuration():
    document = configuration_document()
    for runner in RunnerSettings.objects.all():
        document["projects"].append({"effective": settings_values(runner)})
    return document


@never_cache
@require_GET
def index(request):
    ensure_default_schedule()
    document = presentation_configuration()
    schedules = []
    for schedule in RunnerSchedule.objects.prefetch_related("runners"):
        metadata = {
            "name": schedule.name,
            "mode": schedule.get_schedule_mode_display(),
            "timezone": schedule.timezone,
            "windows": window_labels(schedule),
            "runners": [
                {"pk": runner.pk, "name": str(runner)}
                for runner in schedule.runners.all()
            ],
        }
        safe, error = present_configuration_payload(document, metadata)
        schedules.append({"pk": schedule.pk, "metadata": safe, "error": error})
    return render(request, "runner_control/schedules.html", {"schedules": schedules})


@never_cache
@require_http_methods(["GET", "POST"])
@transaction.atomic
def edit(request, schedule_id=None):
    if request.method == "POST":
        lock_controller()
    ensure_default_schedule()
    list(
        RunnerSettings.objects.select_for_update()
        .filter(pk=1)
        .values_list("pk", flat=True)
    )
    saved = (
        get_object_or_404(RunnerSchedule, pk=schedule_id)
        if schedule_id
        else RunnerSchedule()
    )
    runners = list(RunnerSettings.objects.filter(schedule=saved)) if saved.pk else []
    previous = {runner.pk: settings_values(runner) for runner in runners}
    data = request.POST if request.method == "POST" else None
    form = RunnerScheduleForm(data, instance=saved)
    if request.method == "POST" and form.is_valid():
        form.save()
        for runner in runners:
            observe_settings(
                previous[runner.pk], settings_values(runner), settings_id=runner.pk
            )
        messages.success(
            request,
            "Schedule saved. Changes apply to every assigned coding runner at its next schedule check.",
        )
        return redirect("runner_schedule_edit", schedule_id=saved.pk)
    document = presentation_configuration()
    names = [{"pk": runner.pk, "name": str(runner)} for runner in runners]
    assigned, error = present_configuration_payload(document, names)
    context = {
        "schedule_id": saved.pk,
        "form": form,
        "form_unavailable": form_presentation_error(form, document),
        "assigned_runners": assigned if assigned is not None else [],
        "assigned_error": error,
    }
    return render(request, "runner_control/schedule_edit.html", context)


@never_cache
@require_POST
@transaction.atomic
def delete(request, schedule_id):
    lock_controller()
    list(
        RunnerSettings.objects.select_for_update()
        .filter(pk=1)
        .values_list("pk", flat=True)
    )
    saved = get_object_or_404(RunnerSchedule, pk=schedule_id)
    if saved.pk == 1:
        return HttpResponse("The default schedule cannot be deleted.", status=409)
    try:
        saved.delete()
    except ProtectedError:
        return HttpResponse(
            "Assign the coding runners to another schedule before deleting this schedule.",
            status=409,
        )
    messages.success(request, "Schedule deleted.")
    return redirect("runner_schedule_list")
