from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from runner_control.configuration import configuration_document
from runner_control.settings_forms import RunnerSettingsForm
from runner_control.settings_models import RunnerSettings
from runner_control.gate_models import RunnerGateEvidence
from django.views.decorators.cache import never_cache
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_http_methods

from .forms import APISettingsForm, CredentialForm
from .models import APISettings, Credential


@never_cache
@require_http_methods(["GET", "POST"])
def api_settings(request):
    saved = APISettings.objects.filter(pk=1).first() or APISettings()
    data = request.POST if request.method == "POST" else None
    runner_saved = RunnerSettings.objects.filter(pk=1).first() or RunnerSettings()
    runner_post = request.method == "POST" and request.POST.get("section") == "runner"
    form = APISettingsForm(data if not runner_post else None, instance=saved)
    runner_form = RunnerSettingsForm(
        data if runner_post else None, instance=runner_saved, prefix="runner"
    )
    if runner_post and runner_form.is_valid():
        from runner_control.configuration import settings_values
        from runner_control.release import observe_settings

        previous = RunnerSettings.objects.filter(pk=1).first() or RunnerSettings()
        before = settings_values(previous)
        runner_form.save()
        observe_settings(before, settings_values(runner_form.instance))
        messages.success(
            request,
            "Coding runner settings saved. Live execution remains release-gated.",
        )
        return redirect("api_settings")
    if request.method == "POST" and not runner_post and form.is_valid():
        saved = form.save(commit=False)
        fields = list(form.fields) if not saved._state.adding else None
        saved.save(update_fields=fields)
        messages.success(request, "Settings saved.")
        return redirect("api_settings")
    configuration = configuration_document()
    gate_evidence = list(
        RunnerGateEvidence.objects.select_related("runner").order_by("runner__name")
    )
    for evidence in gate_evidence:
        next_check = parse_datetime(evidence.report["next_check_at"])
        evidence.stale = next_check is None or next_check <= timezone.now()
        evidence.configuration_changed = (
            evidence.report["configuration_revision"] != configuration["revision"]
        )
    from runner_control.models import ReleaseQualification, RunnerIdentity

    context = {
        "runner_identities": RunnerIdentity.objects.filter(enabled=True),
        "release_qualifications": ReleaseQualification.objects.order_by("-created_at")[
            :20
        ],
        "form": form,
        "runner_form": runner_form,
        "runner_configuration": configuration,
        "runner_gate_evidence": gate_evidence,
    }
    return render(request, "credentials/settings.html", context)


@never_cache
@sensitive_post_parameters("secret", "passphrase")
@require_http_methods(["GET", "POST"])
def credential_list(request):
    data = request.POST if request.method == "POST" else None
    form = CredentialForm(data)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Credential saved.")
        return redirect("credential_list")
    credentials = Credential.objects.defer("encrypted_payload")
    context = {"form": form, "credentials": credentials}
    return render(request, "credentials/index.html", context)


@never_cache
@sensitive_post_parameters("secret", "passphrase")
@require_http_methods(["GET", "POST"])
def credential_edit(request, credential_id):
    credential = get_object_or_404(Credential, pk=credential_id)
    form = CredentialForm(
        request.POST if request.method == "POST" else None, instance=credential
    )
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(
            request,
            "Credential updated. Active runs use the current secret on their next retrieval.",
        )
        return redirect("credential_list")
    return render(
        request, "credentials/edit.html", {"form": form, "credential": credential}
    )


@never_cache
@require_http_methods(["POST"])
def credential_revoke(request, credential_id):
    credential = get_object_or_404(Credential, pk=credential_id)
    credential.revoke()
    messages.success(
        request,
        "Credential revoked. Future runner secret retrieval is blocked; stop existing backend processes separately.",
    )
    return redirect("credential_list")
