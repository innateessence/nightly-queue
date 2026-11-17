from django.contrib import messages
from django.db import transaction
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from .configuration import configuration_for_settings, runner_settings, settings_values
from .gate_models import RunnerGateEvidence
from .models import ReleaseQualification, RunnerIdentity
from .plan_presentation import (
    form_presentation_error,
    present_configuration,
    present_configuration_payload,
    present_gates,
    present_release,
)
from .release import observe_settings
from .services import lock_controller
from .settings_forms import RunnerSettingsForm
from .settings_models import RunnerSettings


def window_labels(saved):
    if saved.schedule_mode != "windows":
        return []
    weekdays = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
    labels = []
    for window in saved.windows:
        days = ", ".join(weekdays[day] for day in window["days"])
        labels.append(f'{days} · {window["start"]}–{window["end"]}')
    return labels


@never_cache
@require_GET
def index(request):
    runner_settings()
    runners = []
    for saved in RunnerSettings.objects.select_related("identity", "schedule").order_by(
        "pk"
    ):
        document = configuration_for_settings(saved)
        metadata = {
            "name": str(saved),
            "identity": saved.identity.name if saved.identity else "Unassigned",
            "timezone": saved.schedule.timezone,
            "schedule": saved.schedule.name,
            "windows": window_labels(saved.schedule),
        }
        safe, error = present_configuration_payload(document, metadata)
        runners.append(
            {
                "pk": saved.pk,
                "enabled": saved.enabled,
                "paused": saved.paused,
                "metadata": safe,
                "error": error,
                "schedule_id": saved.schedule_id,
            }
        )
    response = render(
        request, "runner_control/configurations.html", {"runners": runners}
    )
    response["Cache-Control"] = "no-store"
    return response


@never_cache
@require_http_methods(["GET", "POST"])
@transaction.atomic
def edit(request, settings_id=None):
    if request.method == "POST":
        lock_controller()
    runner_settings()
    saved = (
        get_object_or_404(RunnerSettings, pk=settings_id)
        if settings_id
        else RunnerSettings(name="")
    )
    before = settings_values(saved)
    data = request.POST if request.method == "POST" else None
    form = RunnerSettingsForm(data, instance=saved, prefix="runner")
    if request.method == "POST" and form.is_valid():
        form.save()
        observe_settings(before, settings_values(saved), settings_id=saved.pk)
        messages.success(
            request,
            "Coding runner settings saved. Live execution remains release-gated.",
        )
        return redirect("coding_runner_edit", settings_id=saved.pk)
    configuration = configuration_for_settings(saved)
    identities = []
    for runner in RunnerIdentity.objects.filter(enabled=True):
        name, error = present_configuration_payload(configuration, runner.name)
        identities.append(
            {"pk": runner.pk, "name": name if name is not None else "Runner"}
        )
    evidence = RunnerGateEvidence.objects.select_related("runner")
    qualifications = ReleaseQualification.objects.order_by("-created_at")
    if saved.pk == 1:
        evidence = evidence.exclude(runner__configuration__pk__gt=1)
        qualifications = qualifications.exclude(runner__configuration__pk__gt=1)
    else:
        evidence = (
            evidence.filter(runner_id=saved.identity_id)
            if saved.identity_id
            else evidence.none()
        )
        qualifications = (
            qualifications.filter(runner_id=saved.identity_id)
            if saved.identity_id
            else qualifications.none()
        )
    context = {
        "settings_id": saved.pk,
        "runner_form": form,
        "runner_form_unavailable": form_presentation_error(form, configuration),
        "runner_configuration": present_configuration(configuration),
        "runner_gate_evidence": present_gates(evidence, configuration),
        "runner_identities": identities,
        "release_qualifications": [
            present_release(entry, configuration) for entry in qualifications[:20]
        ],
    }
    response = render(request, "runner_control/configuration_edit.html", context)
    response["Cache-Control"] = "no-store"
    return response


@never_cache
@require_POST
@transaction.atomic
def delete(request, settings_id):
    lock_controller()
    saved = get_object_or_404(RunnerSettings, pk=settings_id)
    if saved.pk == 1 or saved.identity_id:
        return HttpResponse(
            "Keep the default configuration. Unassign other configurations before deleting them.",
            status=409,
        )
    saved.delete()
    messages.success(request, "Coding runner deleted.")
    return redirect("coding_runner_list")
