from django.contrib import messages
from django.shortcuts import redirect, render
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
    form = APISettingsForm(data, instance=saved)
    if request.method == "POST" and form.is_valid():
        saved = form.save(commit=False)
        fields = list(form.fields) if not saved._state.adding else None
        saved.save(update_fields=fields)
        messages.success(request, "Settings saved.")
        return redirect("api_settings")
    return render(request, "credentials/settings.html", {"form": form})


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
