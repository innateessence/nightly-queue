from django.contrib import messages
from django.shortcuts import redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_http_methods

from .forms import CredentialForm
from .models import Credential


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
