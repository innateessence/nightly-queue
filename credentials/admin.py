from django.contrib import admin
from django.utils.decorators import method_decorator
from django.views.decorators.debug import sensitive_post_parameters

from .forms import CredentialForm
from .models import Credential


@admin.register(Credential)
class CredentialAdmin(admin.ModelAdmin):
    form = CredentialForm
    list_display = ("id", "name", "kind", "updated_at")
    list_filter = ("kind",)
    search_fields = ("name",)
    readonly_fields = ("id", "created_at", "updated_at")
    fields = ("name", "kind", "secret", "passphrase", "id", "created_at", "updated_at")

    def get_readonly_fields(self, request, obj=None):
        if obj:
            return (*self.readonly_fields, "kind")
        return self.readonly_fields

    @method_decorator(sensitive_post_parameters("secret", "passphrase"))
    def changeform_view(self, request, object_id=None, form_url="", extra_context=None):
        return super().changeform_view(request, object_id, form_url, extra_context)
