import logging

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render

from runner_control.configuration import configuration_document
from runner_control.plan_presentation import form_presentation_error

from django.views.decorators.cache import never_cache
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_http_methods

from .forms import APISettingsForm, CredentialForm
from .models import APISettings, Credential

logger = logging.getLogger(__name__)


@never_cache
@require_http_methods(["GET", "POST"])
def api_settings(request):
    saved = APISettings.objects.filter(pk=1).first() or APISettings()
    data = request.POST if request.method == "POST" else None
    form = APISettingsForm(data, instance=saved)
    if request.method == "POST" and form.is_valid():
        saved = form.save(commit=False)
        fields = list(form.fields) if not saved._state.adding else None
        saved.save(update_fields=fields)
        logger.info("Issue discovery settings saved.")
        messages.success(request, "Settings saved.")
        return redirect("api_settings")
    configuration = configuration_document()
    context = {
        "form": form,
        "form_unavailable": form_presentation_error(form, configuration),
    }
    response = render(request, "credentials/settings.html", context)
    response["Cache-Control"] = "no-store"
    return response


@never_cache
@sensitive_post_parameters("secret", "connection_api_key", "connection_endpoint")
@require_http_methods(["GET", "POST"])
def credential_list(request):
    data = request.POST if request.method == "POST" else None
    form = CredentialForm(data)
    if request.method == "POST" and form.is_valid():
        saved = form.save()
        logger.info("Credential %s created.", saved.pk)
        messages.success(request, "Credential saved.")
        return redirect("credential_list")
    credentials = Credential.objects.defer("encrypted_payload")
    context = {"form": form, "credentials": credentials}
    return render(request, "credentials/index.html", context)


@never_cache
@sensitive_post_parameters("secret", "connection_api_key", "connection_endpoint")
@require_http_methods(["GET", "POST"])
def credential_edit(request, credential_id):
    credential = get_object_or_404(Credential, pk=credential_id)
    form = CredentialForm(
        request.POST if request.method == "POST" else None, instance=credential
    )
    if request.method == "POST" and form.is_valid():
        saved = form.save()
        logger.info("Credential %s updated.", saved.pk)
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
    logger.info("Credential %s revoked.", credential.pk)
    messages.success(
        request,
        "Credential revoked. Future runner secret retrieval is blocked; stop existing backend processes separately.",
    )
    return redirect("credential_list")
