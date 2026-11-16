from django.db.models.deletion import ProtectedError
from django.db.models.signals import pre_delete
from django.dispatch import receiver

from .models import Credential


@receiver(pre_delete, sender=Credential)
def protect_runner_selection(sender, instance, **kwargs):
    from runner_control.settings_models import ProjectRunnerSettings, RunnerSettings

    RunnerSettings.objects.get_or_create(pk=1)
    RunnerSettings.objects.select_for_update().get(pk=1)
    for saved in ProjectRunnerSettings.objects.all():
        selected = any(
            key.endswith("credential_id")
            and entry.get("mode") == "replace"
            and entry.get("value") == instance.pk
            for key, entry in saved.overrides.items()
        )
        if selected:
            raise ProtectedError(
                "Credential is selected by project runner settings.", [instance]
            )
