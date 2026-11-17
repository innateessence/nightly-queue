from django.db.models.signals import pre_delete
from django.dispatch import receiver

from credentials.models import Credential
from projects.models import Project

from .settings_models import ProjectRunnerSettings, RunnerSettings


@receiver(pre_delete, sender=Project)
@receiver(pre_delete, sender=ProjectRunnerSettings)
@receiver(pre_delete, sender=Credential)
@receiver(pre_delete, sender=RunnerSettings)
def lock_configuration_deletion(sender, instance, using, **kwargs):
    list(RunnerSettings.objects.using(using).select_for_update().filter(pk=1))
