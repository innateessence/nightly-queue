"""Explicit, non-destructive self-development configuration."""

from copy import deepcopy
import os
import re

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q

from credentials.models import APISettings, Credential, RunnerPreflightGrant
from projects.models import Project
from projects.validators import normalize_repository_url
from vibearr_runner.policy import ExecutionPolicy
from work_queue.github import repository_metadata
from work_queue.models import QueueItem
from work_queue.services import (
    accept_repository_observation,
    repository_authority_state,
    source_wait_reason,
    verify_repository_authority,
)

from .auth import token_digest
from .configuration import settings_values
from .models import Run, RunnerController, RunnerIdentity
from .settings_models import ProjectRunnerSettings, RunnerSettings


REPOSITORY_ID = 1353963957
OWNER_ID = 17239750
REPOSITORY_URL = "https://github.com/innateessence/nightly-queue.git"
BOOTSTRAP_DEFAULTS = {
    "polling_interval_seconds": 30,
    "implementation_timeout_seconds": 1800,
    "validation_timeout_seconds": 1200,
}
CONFIGURATION_KEYS = {
    "schema_version",
    "import_credential_id",
    "issue_credential_id",
    "repository_credential_id",
    "backend",
    "model",
    "backend_credential_id",
    "runner_name",
    "runner_token_env",
    "execution_policy",
}


def validate_request(value):
    if (
        not isinstance(value, dict)
        or set(value) != CONFIGURATION_KEYS
        or value.get("schema_version") != 1
    ):
        raise ValidationError(
            "Supply the complete version1 bootstrap configuration; secrets do not belong in it."
        )
    if type(value["schema_version"]) is not int:
        raise ValidationError("Bootstrap schema_version must be an integer.")
    for field in (
        "import_credential_id",
        "issue_credential_id",
        "repository_credential_id",
        "backend_credential_id",
    ):
        if type(value[field]) is not int or not 0 < value[field] < 2**63:
            raise ValidationError(
                f"{field} must select an existing positive credential ID."
            )
    if value["import_credential_id"] in {
        value["issue_credential_id"],
        value["repository_credential_id"],
    }:
        raise ValidationError(
            "Select separate import and runner GitHub credential records."
        )
    if (
        value["backend"] != "codex"
        or not isinstance(value["model"], str)
        or not value["model"].strip()
    ):
        raise ValidationError("Select the native Codex backend and an explicit model.")
    if len(value["model"]) > 200:
        raise ValidationError("The selected model is too long.")
    if not isinstance(value["runner_name"], str) or not re.fullmatch(
        r"[-a-zA-Z0-9_]{1,100}", value["runner_name"]
    ):
        raise ValidationError("Choose a runner slug of at most100 characters.")
    if not isinstance(value["runner_token_env"], str) or not re.fullmatch(
        r"[A-Z_][A-Z0-9_]{0,199}", value["runner_token_env"]
    ):
        raise ValidationError(
            "Name the environment variable containing the dedicated runner token."
        )
    policy = ExecutionPolicy.from_data(value["execution_policy"])
    if (
        policy.validation_profile != "vibearr-python312-v1"
        or policy.delivery_mode != "pull_request"
    ):
        raise ValidationError(
            "Bootstrap selects the trusted nine-check validation profile and PR-only delivery."
        )
    return deepcopy(value)


def selected_credentials(request):
    fields = ("import", "issue", "repository", "backend")
    credentials = {}
    for purpose in fields:
        reference = request[f"{purpose}_credential_id"]
        credential = Credential.objects.filter(pk=reference).first()
        if credential is None or credential.revoked_at:
            raise ValidationError(f"Select an existing unrevoked {purpose} credential.")
        provider = request["backend"] if purpose == "backend" else "github"
        kinds = {"token", "session"} if purpose == "backend" else {"token"}
        if credential.provider != provider or credential.kind not in kinds:
            raise ValidationError(
                f"The {purpose} credential has the wrong provider or kind."
            )
        credentials[purpose] = credential
    return credentials


def find_project():
    matches = list(
        Project.objects.filter(
            Q(github_repository_id=REPOSITORY_ID)
            | Q(repository_url__iexact=REPOSITORY_URL)
        )
    )
    if len(matches) > 1:
        raise ValidationError(
            "Multiple projects identify the self repository; reconcile them before bootstrap."
        )
    return matches[0] if matches else None


def desired_overrides(request):
    values = {
        "implementation_backend": request["backend"],
        "implementation_model": request["model"],
        "implementation_credential_id": request["backend_credential_id"],
        "issue_credential_id": request["issue_credential_id"],
        "repository_credential_id": request["repository_credential_id"],
        "execution_policy": request["execution_policy"],
    }
    return {key: {"mode": "replace", "value": value} for key, value in values.items()}


def inspect(request, repository, *, mvp_defaults=False):
    request = validate_request(request)
    credentials = selected_credentials(request)
    if (
        repository["repository_id"] != REPOSITORY_ID
        or repository["owner_id"] != OWNER_ID
        or repository["default_branch"] != "main"
    ):
        raise ValidationError(
            "Provider repository/owner/default branch differs from the self-development manifest."
        )
    project = find_project()
    saved = RunnerSettings.objects.filter(pk=1).first()
    api = APISettings.objects.filter(pk=1).first()
    runner = RunnerIdentity.objects.filter(name=request["runner_name"]).first()
    conflicts = []
    changes = []
    if project:
        expected = {
            "base_branch": "main",
            "autonomous": True,
            "backfill_issues": False,
            "permitted_author_ids": [OWNER_ID],
        }
        for field, value in expected.items():
            if getattr(project, field) != value:
                conflicts.append(
                    f"project.{field}: preserve the saved choice; reconcile explicitly before bootstrap."
                )
        if project.github_repository_id not in {None, REPOSITORY_ID}:
            conflicts.append(
                "project.repository_id: saved repository identity differs."
            )
        if project.autonomous and project.enabled_at is None:
            conflicts.append(
                "project.enabled_at: existing autonomous project lacks its original cutoff; restore it explicitly."
            )
        if project.github_owner_id not in {
            None,
            OWNER_ID,
        } or project.github_default_branch not in {"", "main"}:
            conflicts.append(
                "project.repository_authority: saved owner/default branch differs; reconcile current provider authority explicitly."
            )
    else:
        changes.append(
            "Create autonomous self project with a new current cutoff; backfill remains disabled."
        )
    if api and api.github_credential_id not in {None, credentials["import"].pk}:
        conflicts.append(
            "import_credential_id: preserve the saved import connection; select that ID or reconcile explicitly."
        )
    if api is None or api.github_credential_id is None:
        changes.append("Select the explicitly supplied import credential.")
    defaults = saved or RunnerSettings(**BOOTSTRAP_DEFAULTS)
    if mvp_defaults:
        for field, wanted_value in BOOTSTRAP_DEFAULTS.items():
            current = getattr(defaults, field)
            if current != wanted_value:
                changes.append(
                    f"Explicitly set global {field} from {current} to {wanted_value}."
                )
        if max(BOOTSTRAP_DEFAULTS.values()) > defaults.task_timeout_seconds:
            conflicts.append(
                "global.task_timeout_seconds: explicit MVP stage budgets exceed the saved total deadline; preserve it or change it explicitly first."
            )
    overrides = {}
    if project:
        overrides = (
            ProjectRunnerSettings.objects.filter(project=project)
            .values_list("overrides", flat=True)
            .first()
            or {}
        )
    wanted = desired_overrides(request)
    effective = settings_values(defaults)
    for field, entry in wanted.items():
        existing = overrides.get(field)
        if existing:
            matches = existing == entry
            if existing.get("mode") == "inherit":
                matches = effective[field] == entry["value"]
            if not matches:
                conflicts.append(
                    f"project.{field}: preserve the saved override; reconcile explicitly before bootstrap."
                )
        else:
            current = effective[field]
            empty = current in (None, "", [])
            default_policy = (
                field == "execution_policy"
                and current == RunnerSettings().execution_policy
            )
            if (
                project
                and not empty
                and not default_policy
                and current != entry["value"]
            ):
                conflicts.append(
                    f"project.{field}: global selection differs; choose it or configure the project explicitly."
                )
            else:
                changes.append(f"Select project {field}.")
    if runner and not runner.enabled:
        conflicts.append(
            "runner.enabled: the saved identity is disabled; reconcile explicitly."
        )
    if (
        runner
        and RunnerIdentity.objects.filter(token_digest=runner.token_digest)
        .exclude(pk=runner.pk)
        .exists()
    ):
        conflicts.append(
            "runner.token: the saved digest is assigned to multiple identities; reconcile explicitly."
        )
    token = os.environ.get(request["runner_token_env"], "")
    if token:
        if (
            len(token) < 32
            or any(character.isspace() for character in token)
            or token == settings.NQ_API_TOKEN
        ):
            conflicts.append(
                "runner.token: set a dedicated token of at least32 characters without whitespace."
            )
        elif runner and runner.token_digest != token_digest(token):
            conflicts.append(
                "runner.token: supplied token differs; bootstrap never rotates an existing identity."
            )
        elif (
            RunnerIdentity.objects.filter(token_digest=token_digest(token))
            .exclude(name=request["runner_name"])
            .exists()
        ):
            conflicts.append("runner.token: that token belongs to another identity.")
    elif runner is None:
        conflicts.append(
            "runner.token: set the named private environment variable to provision the missing identity."
        )
    if runner is None:
        changes.append(
            "Provision the selected runner identity without acquiring controller ownership."
        )
    grants = (
        set(
            RunnerPreflightGrant.objects.filter(runner=runner).values_list(
                "credential_id", flat=True
            )
        )
        if runner
        else set()
    )
    for purpose in ("issue", "repository", "backend"):
        if credentials[purpose].pk not in grants:
            changes.append(
                f"Grant selected runner explicit {purpose} credential preflight access."
            )
    if Run.objects.filter(state=Run.State.ACTIVE).exists():
        conflicts.append(
            "controller: an active run exists; finish/reconcile it before applying setup."
        )
    backlog = []
    if project:
        backlog = [
            {
                "number": item.issue_number,
                "status": item.status,
                "reason": source_wait_reason(item),
            }
            for item in QueueItem.objects.filter(project=project).order_by("pk")
        ]
    return {
        "schema_version": 1,
        "applied": False,
        "ready_to_apply": not conflicts,
        "project_id": project.pk if project else None,
        "repository": repository,
        "enabled_at": (
            project.enabled_at.isoformat() if project and project.enabled_at else None
        ),
        "execution_enabled": defaults.enabled,
        "processing_paused": defaults.paused,
        "worker_limit": 1,
        "schedule": defaults.schedule_mode,
        "reserve_enabled": defaults.reserve_enabled,
        "planned_defaults": (
            deepcopy(BOOTSTRAP_DEFAULTS) if mvp_defaults or saved is None else {}
        ),
        "changes": changes,
        "conflicts": conflicts,
        "backlog": backlog,
    }


def bootstrap(request, *, apply=False, mvp_defaults=False):
    request = validate_request(request)
    credentials = selected_credentials(request)
    initial_project = find_project()
    initial_authority = (
        repository_authority_state(initial_project) if initial_project else None
    )
    secret = credentials["import"].get_secret()["secret"]
    repository = repository_metadata(REPOSITORY_URL, secret, REPOSITORY_ID)
    repository["repository_url"] = normalize_repository_url(
        repository["repository_url"]
    )
    preview = inspect(request, repository, mvp_defaults=mvp_defaults)
    if not apply or preview["conflicts"]:
        return preview
    with transaction.atomic():
        RunnerController.objects.select_for_update().get_or_create(pk=1)
        saved, _ = RunnerSettings.objects.get_or_create(
            pk=1, defaults=BOOTSTRAP_DEFAULTS
        )
        RunnerSettings.objects.select_for_update().get(pk=saved.pk)
        list(Credential.objects.select_for_update().values_list("pk", flat=True))
        list(Project.objects.select_for_update().values_list("pk", flat=True))
        list(RunnerIdentity.objects.select_for_update().values_list("pk", flat=True))
        list(APISettings.objects.select_for_update().values_list("pk", flat=True))
        preview = inspect(request, repository, mvp_defaults=mvp_defaults)
        if preview["conflicts"]:
            raise ValidationError(preview["conflicts"])
        changed_defaults = [
            field
            for field, value in BOOTSTRAP_DEFAULTS.items()
            if mvp_defaults and getattr(saved, field) != value
        ]
        if changed_defaults:
            for field in changed_defaults:
                setattr(saved, field, BOOTSTRAP_DEFAULTS[field])
            saved.save(update_fields=changed_defaults)
        project = find_project()
        if project and initial_project is None:
            raise ValidationError(
                "A self project appeared during provider inspection; preview again before applying."
            )
        if initial_project and (project is None or project.pk != initial_project.pk):
            raise ValidationError(
                "Self project identity changed during provider inspection; preview again."
            )
        if project:
            verify_repository_authority(project, initial_authority)
        if project is None:
            project = Project.objects.create(
                name="vibearr self-development",
                repository_url=repository["repository_url"],
                base_branch="main",
                autonomous=True,
                permitted_author_ids=[OWNER_ID],
                backfill_issues=False,
            )
        authority = repository_authority_state(project)
        changed = project.github_repository_id != repository["repository_id"]
        changed = changed or project.github_owner_id != repository["owner_id"]
        changed = (
            changed or project.github_default_branch != repository["default_branch"]
        )
        changed = changed or project.repository_url != repository["repository_url"]
        if changed:
            project = accept_repository_observation(
                project=project, repository=repository, expected=authority
            )
        api, _ = APISettings.objects.get_or_create(pk=1)
        if api.github_credential_id is None:
            api.github_credential_id = request["import_credential_id"]
            api.save(update_fields=["github_credential"])
        overrides, _ = ProjectRunnerSettings.objects.get_or_create(project=project)
        wanted = desired_overrides(request)
        combined = {**wanted, **overrides.overrides}
        if overrides.overrides != combined:
            overrides.overrides = combined
            overrides.save()
        runner = RunnerIdentity.objects.filter(name=request["runner_name"]).first()
        if runner is None:
            runner = RunnerIdentity.objects.create(
                name=request["runner_name"],
                token_digest=token_digest(os.environ[request["runner_token_env"]]),
            )
        for purpose in ("issue", "repository", "backend"):
            RunnerPreflightGrant.objects.get_or_create(
                runner=runner, credential_id=request[f"{purpose}_credential_id"]
            )
        result = inspect(request, repository, mvp_defaults=mvp_defaults)
        result["applied"] = True
        return result
