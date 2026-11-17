from django import forms
from django.contrib import messages
from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods

from credentials.models import Credential
from projects.models import Project
from work_queue.models import QueueItem
from work_queue.services import delete_queue_item

from . import deletion
from .plan_presentation import present_configuration_payload
from .settings_models import RunnerSchedule, RunnerSettings


def safe_label(value, fallback):
    document = {
        "credentials": [
            {"id": pk} for pk in Credential.all_objects.values_list("pk", flat=True)
        ]
    }
    label, error = present_configuration_payload(document, value)
    return label if label is not None else fallback


class ReplacementScheduleForm(forms.Form):
    replacement = forms.ChoiceField(
        label="Move assigned coding runners to",
        error_messages={"invalid_choice": "Choose an available replacement schedule."},
    )

    def __init__(self, *args, schedule, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["replacement"].choices = [
            (saved.pk, safe_label(saved.name, f"Schedule #{saved.pk}"))
            for saved in RunnerSchedule.objects.exclude(pk=schedule.pk)
        ]


def confirmation(
    request, *, kind, saved, cancel, description, action, blocker="", form=None
):
    status = 200
    if request.method == "POST":
        status = 409
        if not blocker and (form is None or form.is_valid()):
            try:
                action()
            except ObjectDoesNotExist:
                raise Http404("This object is no longer available.") from None
            except ValidationError as error:
                blocker = " ".join(error.messages)
            else:
                messages.success(request, f"{kind.capitalize()} deleted.")
                return redirect(cancel)
    name = saved.title if isinstance(saved, QueueItem) else str(saved)
    context = {
        "kind": kind,
        "object_name": safe_label(name, f"{kind.capitalize()} #{saved.pk}"),
        "cancel_url": reverse(cancel),
        "description": description,
        "blocker": blocker,
        "form": form,
    }
    return render(request, "confirm_delete.html", context, status=status)


@never_cache
@require_http_methods(["GET", "POST"])
def runner(request, settings_id):
    saved = get_object_or_404(RunnerSettings, pk=settings_id)
    blocker = (
        "The default runner is required as the fallback configuration and cannot be deleted."
        if saved.pk == 1
        else ""
    )
    return confirmation(
        request,
        kind="coding runner",
        saved=saved,
        cancel="coding_runner_list",
        blocker=blocker,
        description="Delete this runner configuration and disable its assigned machine identity. Existing run history is retained. Stop and reconcile any active work first.",
        action=lambda: deletion.delete_runner(saved.pk),
    )


@never_cache
@require_http_methods(["GET", "POST"])
def schedule(request, schedule_id):
    saved = get_object_or_404(RunnerSchedule, pk=schedule_id)
    blocker = (
        "The default Always on schedule is required for new runners and cannot be deleted."
        if saved.pk == 1
        else ""
    )
    form = None
    if not blocker and saved.runners.exists():
        form = ReplacementScheduleForm(
            request.POST if request.method == "POST" else None,
            schedule=saved,
            initial={"replacement": 1},
        )

    def action():
        deletion.delete_schedule(
            saved.pk, int(form.cleaned_data["replacement"]) if form else None
        )

    return confirmation(
        request,
        kind="schedule",
        saved=saved,
        cancel="runner_schedule_list",
        blocker=blocker,
        form=form,
        description="Delete this schedule. Assigned coding runners will use the replacement schedule at their next schedule check. Their other settings are preserved.",
        action=action,
    )


@never_cache
@require_http_methods(["GET", "POST"])
def credential(request, credential_id):
    saved = get_object_or_404(Credential, pk=credential_id)
    return confirmation(
        request,
        kind="credential",
        saved=saved,
        cancel="credential_list",
        description="Remove this credential from saved credentials and all current selections. Affected coding runners are disabled and paused; projects using it for issue imports have automatic imports disabled. Preflight grants are removed. Active work must be stopped first. Encrypted material needed to redact retained run history is preserved with access revoked; otherwise the secret and its saved versions are erased. This does not revoke the credential at its provider.",
        action=lambda: deletion.delete_credential(saved.pk),
    )


@never_cache
@require_http_methods(["GET", "POST"])
def project(request, pk):
    saved = get_object_or_404(Project, pk=pk)
    count = saved.items.count()
    return confirmation(
        request,
        kind="project",
        saved=saved,
        cancel="project_list",
        description=f"Delete this project and remove its {count} work items from the queue. Automatic imports stop. Run history and the records it needs are retained. Stop and reconcile any running work first. The remote repository and its issues are unchanged.",
        action=lambda: deletion.delete_project(saved.pk),
    )


@never_cache
@require_http_methods(["GET", "POST"])
def item(request, pk):
    saved = get_object_or_404(QueueItem, pk=pk)
    return confirmation(
        request,
        kind="work item",
        saved=saved,
        cancel="index",
        description="Remove this item from the queue. Its run history is retained. Imported GitHub issues stay open remotely and will not be added again by later syncs. Stop and requeue running work first.",
        action=lambda: delete_queue_item(saved),
    )
