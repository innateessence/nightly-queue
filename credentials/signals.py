from django.db.models.deletion import ProtectedError
from django.db.models.signals import pre_delete
from django.dispatch import receiver

from .models import Credential


def selected_project_references(credential_ids):
    from runner_control.settings_models import ProjectRunnerSettings

    for saved in ProjectRunnerSettings.objects.select_related("project"):
        selected = any(
            key.endswith("credential_id")
            and entry.get("mode") == "replace"
            and entry.get("value") in credential_ids
            for key, entry in saved.overrides.items()
        )
        if selected:
            yield saved


@receiver(pre_delete, sender=Credential)
def protect_runner_selection(sender, instance, **kwargs):
    from runner_control.settings_models import RunnerSettings

    RunnerSettings.objects.get_or_create(pk=1)
    RunnerSettings.objects.select_for_update().get(pk=1)
    if next(selected_project_references({instance.pk}), None) is not None:
        raise ProtectedError(
            "Credential is selected by project runner settings.", [instance]
        )
