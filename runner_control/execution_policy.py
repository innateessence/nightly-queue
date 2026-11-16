from django.db import transaction

from vibearr_runner.policy import ExecutionPolicy, PolicyError

from .configuration import effective_project_configuration
from .models import Run
from .services import RunConflict, lock_controller, verify_active
from .settings_models import RunnerSettings


@transaction.atomic
def current_native_policy(run, claim_token):
    """Policy binding only; runtime, schedule and quota gates remain required."""
    lock_controller()
    try:
        run = Run.objects.select_related("item__project").get(pk=run.pk)
    except Run.DoesNotExist:
        raise RunConflict("Run no longer exists.") from None
    verify_active(run, claim_token)
    snapshot = run.configuration_snapshot
    if snapshot.get("execution_kind") != "native":
        raise RunConflict("Legacy proposal execution is disabled.")
    settings = RunnerSettings.objects.get(pk=1)
    if not settings.enabled or settings.paused:
        raise RunConflict("Native execution is disabled or paused.")
    try:
        policy = ExecutionPolicy.from_data(snapshot.get("execution_policy"))
    except PolicyError:
        raise RunConflict("The native policy snapshot is invalid.") from None
    current = effective_project_configuration(run.item.project)
    if (
        snapshot.get("policy_digest") != policy.digest
        or current["policy_digest"] != policy.digest
    ):
        raise RunConflict("Execution policy changed; stop and reconcile the run.")
    return policy
