from django.contrib import admin
from django.db import transaction
from django.utils.decorators import method_decorator
from django.views.decorators.debug import sensitive_post_parameters

from .forms import CredentialForm
from .models import Credential, RunnerPreflightGrant
from .signals import selected_project_references, selected_run_references


@admin.register(Credential)
class CredentialAdmin(admin.ModelAdmin):
    form = CredentialForm
    list_display = ("id", "name", "provider", "kind", "updated_at")
    list_filter = ("provider", "kind")
    search_fields = ("name",)
    readonly_fields = ("id", "created_at", "updated_at")
    fields = (
        "name",
        "provider",
        "kind",
        "secret",
        "passphrase",
        "id",
        "created_at",
        "updated_at",
    )

    @transaction.atomic
    def get_deleted_objects(self, objs, request):
        from runner_control.settings_models import RunnerSettings

        RunnerSettings.objects.get_or_create(pk=1)
        RunnerSettings.objects.select_for_update().get(pk=1)
        objs = list(objs)
        deleted, counts, permissions, protected = super().get_deleted_objects(
            objs, request
        )
        references = {obj.pk for obj in objs}
        selected = selected_project_references(references)
        protected.extend(
            f"Project runner settings: {saved.project}" for saved in selected
        )
        runs = selected_run_references(references)
        protected.extend(f"Runner run configuration: {run.pk}" for run in runs)
        return deleted, counts, permissions, protected

    @transaction.atomic
    def changelist_view(self, request, extra_context=None):
        return super().changelist_view(request, extra_context)

    def get_readonly_fields(self, request, obj=None):
        if obj:
            return (*self.readonly_fields, "kind")
        return self.readonly_fields

    @method_decorator(sensitive_post_parameters("secret", "passphrase"))
    def changeform_view(self, request, object_id=None, form_url="", extra_context=None):
        return super().changeform_view(request, object_id, form_url, extra_context)


@admin.register(RunnerPreflightGrant)
class RunnerPreflightGrantAdmin(admin.ModelAdmin):
    list_display = ("runner", "credential")
