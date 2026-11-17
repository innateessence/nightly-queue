from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from credentials.models import (
    APISettings,
    Credential,
    CredentialSecretVersion,
    RunnerPreflightGrant,
)
from credentials.references import snapshot_credential_ids
from credentials.signals import selected_run_references
from projects.models import Project
from work_queue.models import QueueItem

from .configuration import runner_settings, settings_values
from .models import Run, RunnerIdentity
from .release import observe_settings
from .services import lock_controller
from .settings_models import ProjectRunnerSettings, RunnerSchedule, RunnerSettings


def lock_configuration():
    lock_controller()
    runner_settings()
    RunnerSettings.objects.select_for_update().get(pk=1)


@transaction.atomic
def delete_runner(settings_id):
    lock_configuration()
    saved = RunnerSettings.objects.select_for_update().get(pk=settings_id)
    if saved.pk == 1:
        raise ValidationError(
            "The default runner is required as the fallback configuration and cannot be deleted."
        )
    if saved.identity_id:
        if Run.objects.filter(
            runner_id=saved.identity_id, state=Run.State.ACTIVE
        ).exists():
            raise ValidationError(
                "Stop and reconcile this runner's active work before deleting it."
            )
        RunnerIdentity.objects.filter(pk=saved.identity_id).update(enabled=False)
    saved.delete()


@transaction.atomic
def delete_schedule(schedule_id, replacement_id=None):
    lock_configuration()
    saved = RunnerSchedule.objects.select_for_update().get(pk=schedule_id)
    if saved.pk == 1:
        raise ValidationError(
            "The default Always on schedule is required for new runners and cannot be deleted."
        )
    runners = list(RunnerSettings.objects.filter(schedule=saved))
    if runners:
        replacement = (
            RunnerSchedule.objects.filter(pk=replacement_id)
            .exclude(pk=saved.pk)
            .first()
        )
        if replacement is None:
            raise ValidationError(
                "Choose another schedule for the assigned coding runners before deleting this schedule."
            )
        for runner in runners:
            before = settings_values(runner)
            RunnerSettings.objects.filter(pk=runner.pk).update(schedule=replacement)
            runner.schedule = replacement
            observe_settings(before, settings_values(runner), settings_id=runner.pk)
    saved.delete()


@transaction.atomic
def delete_project(project_id):
    lock_configuration()
    project = Project.objects.select_for_update().get(pk=project_id)
    running = QueueItem.objects.filter(
        project=project, status=QueueItem.Status.RUNNING
    ).exists()
    if (
        running
        or Run.objects.filter(item__project=project, state=Run.State.ACTIVE).exists()
    ):
        raise ValidationError(
            "Stop and reconcile this project's running work before deleting it."
        )
    now = timezone.now()
    QueueItem.objects.filter(project=project).update(deleted_at=now)
    ProjectRunnerSettings.objects.filter(project=project).delete()
    Project.objects.filter(pk=project.pk).update(
        deleted_at=now,
        autonomous=False,
        credential=None,
        sync_lock_token=None,
        sync_lock_until=None,
    )


@transaction.atomic
def delete_credential(credential_id):
    lock_configuration()
    saved = Credential.objects.select_for_update().get(pk=credential_id)
    active = Run.objects.filter(state=Run.State.ACTIVE).only("configuration_snapshot")
    if any(
        saved.pk in snapshot_credential_ids(run.configuration_snapshot)
        for run in active
    ):
        raise ValidationError(
            "Stop and reconcile active work using this credential before deleting it."
        )
    if APISettings.objects.filter(github_credential=saved).exists():
        Project.objects.select_for_update().filter(credential__isnull=True).update(
            autonomous=False, sync_lock_token=None, sync_lock_until=None
        )
    for field in ("github_credential", "gitlab_credential"):
        APISettings.objects.filter(**{field: saved}).update(**{field: None})
    Project.objects.select_for_update().filter(credential=saved).update(
        credential=None, autonomous=False, sync_lock_token=None, sync_lock_until=None
    )
    for field in (
        "research_credential",
        "implementation_credential",
        "issue_credential",
        "repository_credential",
    ):
        for runner in RunnerSettings.objects.filter(**{field: saved}):
            before = settings_values(runner)
            RunnerSettings.objects.filter(pk=runner.pk).update(
                **{field: None, "enabled": False, "paused": True}
            )
            setattr(runner, field, None)
            runner.enabled, runner.paused = False, True
            observe_settings(before, settings_values(runner), settings_id=runner.pk)
    for settings in ProjectRunnerSettings.objects.select_for_update():
        overrides = dict(settings.overrides)
        for field, entry in overrides.items():
            if (
                field.endswith("credential_id")
                and entry.get("mode") == "replace"
                and entry.get("value") == saved.pk
            ):
                overrides[field] = {"mode": "clear"}
        if overrides != settings.overrides:
            ProjectRunnerSettings.objects.filter(pk=settings.pk).update(
                overrides=overrides
            )
    RunnerPreflightGrant.objects.filter(credential=saved).delete()
    retained = next(selected_run_references({saved.pk}), None) is not None
    if retained:
        Credential.objects.filter(pk=saved.pk).update(
            deleted_at=timezone.now(), revoked_at=saved.revoked_at or timezone.now()
        )
        return
    CredentialSecretVersion.objects.filter(credential=saved).delete()
    saved.delete()
