"""Read-only setup diagnostics; no claims, probes, or credential changes."""

import shutil
import os
from datetime import datetime, timezone

from django.core.exceptions import ValidationError

from credentials.models import RunnerPreflightGrant
from vibearr_runner.native_sessions import NativeBackendRegistry
from vibearr_runner.native_runtime import runtime_diagnostics
from vibearr_runner.native_validation import (
    load_validation_profile,
    validation_diagnostics,
)
from vibearr_runner.policy import ExecutionPolicy
from vibearr_runner.gates import schedule

from .auth import token_digest
from .bootstrap import find_project, inspect, selected_credentials, validate_request
from .configuration import project_configuration, settings_values
from .models import RunnerIdentity
from .settings_models import ProjectRunnerSettings, RunnerSettings


def diagnose(
    request,
    repository,
    *,
    queue=None,
    executor=None,
    github=None,
    journal=None,
    state_directory=None,
    profile_path=None,
    failures=None,
):
    request = validate_request(request)
    components = []
    journal = journal or {}

    def add(component, status, detail, fix=""):
        components.append(
            {"component": component, "status": status, "detail": detail, "fix": fix}
        )

    try:
        credentials = selected_credentials(request)
        preview = inspect(request, repository)
    except ValidationError as exc:
        credentials = {}
        project = find_project()
        saved = RunnerSettings.objects.filter(pk=1).first() or RunnerSettings()
        preview = {
            "conflicts": exc.messages,
            "project_id": project.pk if project else None,
            "enabled_at": (
                project.enabled_at.isoformat()
                if project and project.enabled_at
                else None
            ),
            "execution_enabled": saved.enabled,
            "processing_paused": saved.paused,
            "schedule": settings_values(saved)["schedule_mode"],
            "reserve_enabled": saved.reserve_enabled,
            "backlog": [],
        }
        from credentials.models import Credential

        for purpose in ("import", "issue", "repository", "backend"):
            credential = Credential.objects.filter(
                pk=request[f"{purpose}_credential_id"]
            ).first()
            provider = request["backend"] if purpose == "backend" else "github"
            kinds = {"token", "session"} if purpose == "backend" else {"token"}
            valid = (
                credential
                and not credential.revoked_at
                and credential.provider == provider
                and credential.kind in kinds
            )
            add(
                f"credential.{purpose}",
                "ready" if valid else "error",
                (
                    f"Selected credential#{request[f'{purpose}_credential_id']} is usable."
                    if valid
                    else f"Selected {purpose} credential is missing, revoked, or has the wrong provider/kind."
                ),
                "Save a credential outside bootstrap and explicitly select its ID; no host secret discovery occurs.",
            )
            if valid:
                credentials[purpose] = credential

    add(
        "configuration",
        "error" if preview["conflicts"] else "ready",
        "; ".join(preview["conflicts"]) or "Saved choices are compatible.",
        "Resolve each saved-choice conflict explicitly; bootstrap never replaces it.",
    )
    add(
        "execution",
        (
            "ready"
            if preview["execution_enabled"] and not preview["processing_paused"]
            else "wait"
        ),
        (
            "Global processing is enabled."
            if preview["execution_enabled"] and not preview["processing_paused"]
            else f'Global processing enabled={preview["execution_enabled"]}; paused={preview["processing_paused"]}.'
        ),
        "Enable processing once setup and finite acceptance are ready.",
    )
    controls = RunnerSettings.objects.filter(pk=1).first() or RunnerSettings()
    interval, boundary = schedule(settings_values(controls), datetime.now(timezone.utc))
    started = (
        journal.get("gates", {}).get("counts", {}).get(interval.id, 0)
        if interval
        else 0
    )
    add(
        "schedule",
        "ready" if interval and started < controls.tasks_per_window else "wait",
        f'One active run; schedule={preview["schedule"]}; task-window starts={started}/{controls.tasks_per_window}; next boundary={boundary.isoformat() if boundary else None}.',
        "Normal processing honors the saved window; bootstrap does not change it.",
    )
    add(
        "usage_reserve",
        "wait" if preview["reserve_enabled"] else "ready",
        (
            "The selected native Codex account has no supported allowance telemetry; enabled reserve enforcement waits safely."
            if preview["reserve_enabled"]
            else "Usage reserves are disabled."
        ),
        "Keep the saved reserve or explicitly disable it; token usage is not remaining allowance.",
    )
    project = find_project()
    if project is None:
        add(
            "project",
            "error",
            "The self project has not been saved.",
            "Review bootstrap preview, then apply it once.",
        )
    else:
        saved = RunnerSettings.objects.filter(pk=1).first() or RunnerSettings()
        overrides = (
            ProjectRunnerSettings.objects.filter(project=project)
            .values_list("overrides", flat=True)
            .first()
            or {}
        )
        metadata = {
            value.pk: {
                "provider": value.provider,
                "kind": value.kind,
                "revoked_at": value.revoked_at,
            }
            for value in credentials.values()
        }
        effective = project_configuration(
            project, settings_values(saved), overrides, metadata
        )
        errors = [value["message"] for value in effective["errors"]]
        add(
            "project",
            "error" if errors else "ready",
            "; ".join(errors)
            or f'Project#{project.pk}; original cutoff {preview["enabled_at"]}; historical backfill disabled.',
            "Apply missing selections without replacing the saved cutoff.",
        )
    runner = RunnerIdentity.objects.filter(name=request["runner_name"]).first()
    journal = journal or {}
    identity = journal.get("runner_id")
    proof = journal.get("controller_secret")
    owned = bool(
        runner
        and identity == runner.pk
        and isinstance(proof, str)
        and runner.controller_digest == token_digest(proof)
    )
    add(
        "controller_ownership",
        "ready" if owned else "wait",
        (
            "Private journal matches the saved runner/controller proof."
            if owned
            else "Controller ownership is not proven by this private journal."
        ),
        "Use this runner's original private journal; normal startup binds a new unowned identity. Never copy another controller's proof.",
    )
    selected = {
        request[f"{purpose}_credential_id"]
        for purpose in ("issue", "repository", "backend")
    }
    grants = (
        set(
            RunnerPreflightGrant.objects.filter(runner=runner).values_list(
                "credential_id", flat=True
            )
        )
        if runner
        else set()
    )
    add(
        "preflight_grants",
        "ready" if selected <= grants else "error",
        (
            "Selected runner preflight grants are installed."
            if selected <= grants
            else f"Missing explicit preflight grant credential IDs: {sorted(selected - grants)}."
        ),
        "Apply the bootstrap to grant the selected runner these credential IDs.",
    )
    backend = credentials.get("backend")
    if backend:
        add(
            "backend_selection",
            "ready",
            f'{request["backend"]}; credential#{backend.pk} version{backend.secret_version}; kind={backend.kind}; model={request["model"]}.',
        )
    if queue is None:
        add(
            "api_identity",
            "wait",
            "No authenticated API observation requested.",
            "Run with --online and the dedicated runner token/URL.",
        )
    else:
        try:
            response = queue.identity()
            if not runner or response.get("runner", {}).get("id") != runner.pk:
                raise ValueError("API identity differs from the selected saved runner.")
            add(
                "api_identity",
                "ready",
                "The API authenticated the explicitly selected runner identity.",
            )
        except Exception as exc:
            add(
                "api_identity",
                "error",
                f"API identity check failed ({type(exc).__name__}).",
                "Correct the dedicated runner API token/URL; do not use the generic queue token.",
            )
    if github is None:
        add(
            "provider_permissions",
            "wait",
            "Repository/PR/check reads have not been observed.",
            "Run --online using the explicitly selected repository credential.",
        )
    else:
        try:
            observed, permissions = github.repository(
                repository_id=repository["repository_id"]
            )
            if any(
                observed[key] != repository[key]
                for key in ("repository_id", "owner_id", "default_branch")
            ):
                raise ValueError("Repository identity differs.")
            name = observed["repository"]
            github.request("GET", f"/repos/{name}/pulls?state=open&per_page=1")
            github.request(
                "GET",
                f'/repos/{name}/commits/{observed["default_branch"]}/check-runs?per_page=1',
            )
            github.request(
                "GET", f'/repos/{name}/commits/{observed["default_branch"]}/status'
            )
            if permissions.get("push") is False:
                raise ValueError("Provider reports no push permission.")
            add(
                "provider_permissions",
                "ready",
                "Repository, PR and check/status reads passed. Token contents/PR write scope is proven only by actual delivery; workflow edits may require additional scope.",
                "Use the finite PR-only acceptance to prove writes. Do not broaden credentials silently.",
            )
        except Exception as exc:
            add(
                "provider_permissions",
                "error",
                f"Repository/PR/check read or push-role check failed: {str(exc) or type(exc).__name__}.",
                "Select a credential with issue reads, contents/PR writes and check/status reads. Merge policy remains feature07.",
            )
    policy = ExecutionPolicy.from_data(request["execution_policy"])
    try:
        profile = load_validation_profile(profile_path)
        add(
            "validation_profile",
            "ready",
            f"{profile.profile_id}; image={profile.image}; database={profile.database_image}; recipe={profile.digest}; nine required checks.",
        )
    except Exception as exc:
        profile = None
        add(
            "validation_profile",
            "error",
            f"Trusted profile is unavailable ({type(exc).__name__}).",
            "Select a private immutable VIBEARR_VALIDATION_PROFILE_FILE generated from reviewed requirements.",
        )
    if executor is None:
        add(
            "runtime",
            "wait",
            "No installed executor observation requested.",
            "Run --online with the authenticated dedicated executor URL/token.",
        )
        add(
            "validation_runtime",
            "wait",
            "Installed nine-check profile/database binding has not been observed.",
            "Configure the selected executor, then run --online.",
        )
        add(
            "native_authentication",
            "wait",
            "Selected-account authentication/profile proof has not been observed.",
            "Configure the executor and explicit preflight grants; ordinary eligible-work startup performs the bounded paid probe.",
        )
    else:
        runtime = runtime_diagnostics(
            policy.to_data(), executor, validation_profile=profile
        )
        add(
            "runtime",
            "ready" if runtime["runtime_ready"] else "error",
            runtime["reason"],
            "Qualify current images/profiles/runtime on the owned executor host; production-run evidence is unnecessary.",
        )
        validation = (
            validation_diagnostics(policy, executor, profile)
            if profile
            else {"ready": False, "reason": "Trusted validation profile is missing."}
        )
        add(
            "validation_runtime",
            "ready" if validation["ready"] else "error",
            validation["reason"],
            "Register all nine fixed validation profiles and the setup profile; qualify every distinct immutable image/database.",
        )
        if backend and selected <= grants:
            try:
                adapter = NativeBackendRegistry().create(request["backend"], executor)
                grant = {
                    "credential": {
                        "id": backend.pk,
                        "version": backend.secret_version,
                        "provider": backend.provider,
                        "kind": backend.kind,
                    },
                    "material": backend.get_secret(),
                }
                proofs = (
                    journal.get("native_preflight", {}).get("accounts", {}).values()
                )
                cached = next(
                    (
                        value.get("proof")
                        for value in proofs
                        if value.get("proof", {}).get("account")
                        == {
                            "backend": request["backend"],
                            "credential_id": backend.pk,
                            "credential_version": backend.secret_version,
                            "model": request["model"],
                        }
                    ),
                    None,
                )
                diagnostic = adapter.diagnostics(
                    policy.to_data(), grant, model=request["model"], proof=cached
                )
                add(
                    "native_authentication",
                    "ready" if diagnostic.get("native_agent_ready") else "wait",
                    diagnostic["reason"],
                    "Normal eligible-work preflight runs the bounded selected-account probe and caches its exact model/profile/runtime proof. Diagnostics never make paid calls.",
                )
            except Exception as exc:
                add(
                    "native_authentication",
                    "error",
                    f"Selected native adapter/authentication check failed ({type(exc).__name__}).",
                    "Correct the selected account/model/profile. API-key auth is preferred; an explicitly selected non-refreshing session is a separate capability.",
                )
        else:
            add(
                "native_authentication",
                "error",
                "Selected native account or explicit preflight grants are missing.",
                "Select the credential and grant this runner explicit preflight access; diagnostics do not fetch ungranted material.",
            )
    if state_directory is None or not state_directory.is_dir():
        add(
            "storage",
            "wait",
            "An existing private runner state directory was not supplied.",
            "Create the dedicated private state directory and pass --state; do not copy a live checkout/database.",
        )
    else:
        info = state_directory.lstat()
        private = (
            not state_directory.is_symlink()
            and info.st_uid == os.getuid()
            and info.st_mode & 0o077 == 0
        )
        free = shutil.disk_usage(state_directory).free
        minimum = policy.workspace_mb * 1024 * 1024
        add(
            "storage",
            "ready" if private and free >= minimum else "error",
            f"Free bytes={free}; minimum workspace bytes={minimum}; private={private}.",
            "Use an owned0700 state directory with enough free capacity; executor capacity is checked independently.",
        )
    for component, detail in (failures or {}).items():
        components[:] = [
            value for value in components if value["component"] != component
        ]
        add(
            component,
            "error",
            detail,
            "Correct this selected component's connection before claiming; diagnostics never change secrets or permissions.",
        )
    return {
        "schema_version": 1,
        "read_only": True,
        "model_calls": 0,
        "project_id": preview["project_id"],
        "enabled_at": preview["enabled_at"],
        "ready": all(value["status"] == "ready" for value in components),
        "components": components,
        "backlog": preview["backlog"],
    }
