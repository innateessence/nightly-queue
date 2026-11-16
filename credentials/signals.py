from django.db.models.deletion import ProtectedError
from django.db.models.signals import pre_delete
from django.dispatch import receiver

from .models import Credential
from .references import snapshot_credential_ids


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


def selected_run_references(credential_ids):
    from runner_control.models import Run

    runs = Run.objects.only("id", "configuration_snapshot").iterator()
    for run in runs:
        if snapshot_credential_ids(run.configuration_snapshot) & credential_ids:
            yield run


@receiver(pre_delete, sender=Credential)
def protect_runner_selection(sender, instance, **kwargs):
    from runner_control.settings_models import RunnerSettings

    RunnerSettings.objects.get_or_create(pk=1)
    RunnerSettings.objects.select_for_update().get(pk=1)
    if next(selected_project_references({instance.pk}), None) is not None:
        raise ProtectedError(
            "Credential is selected by project runner settings.", [instance]
        )
    if next(selected_run_references({instance.pk}), None) is not None:
        raise ProtectedError(
            "Credential is selected by a retained run configuration snapshot.",
            [instance],
        )
