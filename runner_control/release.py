import json
import re
from datetime import timedelta

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from coding_runner.backend_transports import PROOF_PROTOCOL, TRANSPORTS

from .models import ReleaseQualification, Run
from .services import RunConflict, json_digest, lock_controller
from coding_runner.release_evidence import valid_response_id, verify_boundary_workflow

TRANSPORT = "codex-responses-structured-proposals-v1"
CONTROL_FIELDS = {
    "enabled",
    "paused",
    "schedule_mode",
    "timezone",
    "windows",
    "tasks_per_window",
    "reserve_enabled",
    "minimum_remaining_usage_percent",
    "polling_interval_seconds",
    "workspace_retention_days",
    "plan_retention_days",
    "log_retention_days",
}


def scope(document):
    defaults = {
        key: value
        for key, value in document["global"].items()
        if key not in CONTROL_FIELDS
    }
    credentials = [
        {
            key: row[key]
            for key in ("id", "provider", "kind", "secret_version", "revoked_at")
        }
        for row in document["credentials"]
    ]
    projects = [
        {
            key: row[key]
            for key in ("project_id", "repository_url", "base_branch", "effective")
        }
        for row in document["projects"]
    ]
    return json_digest(
        {
            "global": defaults,
            "credentials": credentials,
            "projects": projects,
            "transport": PROOF_PROTOCOL,
            "transports": TRANSPORTS,
        }
    )


def accounts(document):
    versions = {row["id"]: row["secret_version"] for row in document["credentials"]}
    values = []
    configurations = [
        document["global"],
        *[row["effective"] for row in document["projects"]],
    ]
    for effective in configurations:
        for stage in ("research", "implementation"):
            entry = {
                "backend": effective[f"{stage}_backend"],
                "credential_id": effective[f"{stage}_credential_id"],
                "model": effective[f"{stage}_model"],
            }
            entry["secret_version"] = versions.get(entry["credential_id"])
            entry["transport"] = TRANSPORTS.get(entry["backend"])
            if entry["backend"] and entry not in values:
                values.append(entry)
    return values


def verification(document):
    entry = (
        ReleaseQualification.objects.filter(
            scope_digest=scope(document),
            qualified_at__isnull=False,
            revoked_at__isnull=True,
            expires_at__gt=timezone.now(),
        )
        .order_by("-qualified_at")
        .first()
    )
    if entry is None:
        return {
            "qualified": False,
            "reason": "Controlled live release verification is missing, expired, revoked, or no longer matches credentials/models/policy.",
        }
    return {
        "qualified": True,
        "qualification_id": str(entry.pk),
        "runner_id": entry.runner_id,
        "scope_digest": entry.scope_digest,
        "environment_digest": entry.environment_digest,
        "code_revision": entry.code_revision,
        "expires_at": entry.expires_at.isoformat(),
        "transport": PROOF_PROTOCOL,
        "accounts": accounts(document),
    }


@transaction.atomic
def authorize(runner, repository_url, environment_digest, code_revision, note):
    from .configuration import configuration_document

    lock_controller()
    if not re.fullmatch(
        r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository_url
    ):
        raise ValidationError(
            "Use a designated GitHub test repository without a trailing slash."
        )
    if not all(
        re.fullmatch(r"[0-9a-f]{64}", value)
        for value in (environment_digest, code_revision)
    ):
        raise ValidationError(
            "Use SHA-256 deployment binding values from the release harness probe."
        )
    if not note.strip():
        raise ValidationError(
            "Record explicit authorization for the test repository and live backend calls."
        )
    document = configuration_document()
    projects = [
        row for row in document["projects"] if row["repository_url"] == repository_url
    ]
    if any(not row["transport"] for row in accounts(document)):
        raise ValidationError(
            "This controlled harness requires a supported structured-proposal API transport."
        )
    if not projects or any(not row["structurally_valid"] for row in projects):
        raise ValidationError(
            "The designated test project must have complete runner configuration."
        )
    return ReleaseQualification.objects.create(
        runner=runner,
        repository_url=repository_url,
        scope_digest=scope(document),
        environment_digest=environment_digest,
        code_revision=code_revision,
        authorization_note=note,
        expires_at=timezone.now() + timedelta(hours=24),
        evidence={"ui_changes": [], "gates": []},
    )


def observe_settings(before, after):
    changed = sorted(key for key in CONTROL_FIELDS if before.get(key) != after.get(key))
    if not changed:
        return
    for entry in ReleaseQualification.objects.filter(
        qualified_at__isnull=True,
        revoked_at__isnull=True,
        expires_at__gt=timezone.now(),
    ):
        with transaction.atomic():
            entry = ReleaseQualification.objects.select_for_update().get(pk=entry.pk)
            events = entry.evidence.setdefault("ui_changes", [])
            events.append(
                {
                    "at": timezone.now().isoformat(),
                    "fields": changed,
                    "values": {key: after.get(key) for key in changed},
                }
            )
            entry.evidence["ui_changes"] = events[-100:]
            entry.save(update_fields=["evidence"])


def observe_gate(runner, report):
    for entry in ReleaseQualification.objects.filter(
        runner=runner,
        qualified_at__isnull=True,
        revoked_at__isnull=True,
        expires_at__gt=timezone.now(),
    ):
        with transaction.atomic():
            entry = ReleaseQualification.objects.select_for_update().get(pk=entry.pk)
            events = entry.evidence.setdefault("gates", [])
            events.append(report)
            entry.evidence["gates"] = events[-100:]
            entry.save(update_fields=["evidence"])


def authorized(entry, runner, document):
    if (
        entry.runner_id != runner.pk
        or entry.revoked_at
        or entry.expires_at <= timezone.now()
    ):
        raise RunConflict("Controlled release authorization is unavailable or expired.")
    if entry.scope_digest != scope(document):
        raise RunConflict(
            "Controlled release credentials, model, code, or policy changed."
        )


def qualified_run(entry, run_id, result):
    try:
        run = Run.objects.get(
            pk=run_id,
            runner=entry.runner,
            state=Run.State.COMPLETED,
            result_status=result,
        )
    except (Run.DoesNotExist, ValueError, ValidationError):
        raise RunConflict(
            "Release evidence must name an owned, completed scenario run."
        ) from None
    repository = run.configuration_snapshot.get("repository_url")
    if repository != entry.repository_url or run.created_at < entry.created_at:
        raise RunConflict(
            "Scenario run is outside the authorized repository or time range."
        )
    return run


@transaction.atomic
def complete(entry_id, runner, report):
    from .configuration import configuration_document

    lock_controller(runner)
    entry = ReleaseQualification.objects.select_for_update().get(
        pk=entry_id, runner=runner
    )
    authorized(entry, runner, configuration_document())
    if not isinstance(report, dict):
        raise RunConflict("Release evidence must be an object.")
    if entry.qualified_at:
        if entry.evidence.get("live_report") != report:
            raise RunConflict("Qualified release evidence is immutable.")
        return entry
    if report.get("mode") != "live":
        raise RunConflict("Replay evidence cannot qualify live execution.")
    if (
        report.get("environment_digest") != entry.environment_digest
        or report.get("code_revision") != entry.code_revision
    ):
        raise RunConflict("Release deployment binding changed.")
    boundary = report.get("boundary", {})
    environment = boundary.get("environment", {}) if isinstance(boundary, dict) else {}
    if (
        not isinstance(environment, dict)
        or json_digest(environment) != entry.environment_digest
    ):
        raise RunConflict(
            "Deployment boundary evidence must match the authorized environment."
        )
    image = environment.get("image_digest", "")
    policy = environment.get("operator_policy", "")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", image) or not re.fullmatch(
        r"[0-9a-f]{64}", policy
    ):
        raise RunConflict(
            "Bind the actual immutable runner image and reviewed sandbox policy artifact."
        )
    if (
        boundary.get("command")
        != ["python", "-m", "coding_runner.verify", "--boundary"]
        or boundary.get("exit_code") != 0
        or not boundary.get("output_digest")
    ):
        raise RunConflict(
            "Full OS boundary acceptance must actually pass in the bound deployment."
        )
    if not re.fullmatch(
        r"https://github.com/[^/]+/[^/]+/actions/runs/[1-9][0-9]*",
        boundary.get("workflow_run_url", ""),
    ):
        raise RunConflict("Record the exact deployment-boundary workflow result URL.")
    success = qualified_run(entry, report.get("success_run_id"), "succeeded")
    failure = qualified_run(entry, report.get("failure_run_id"), "failed")
    from .plan_presentation import present_configuration_payload

    safe, error = present_configuration_payload(
        configuration_document(),
        report,
        runs=[success, failure],
        preserve_secret_versions=True,
    )
    if error or safe != report:
        raise RunConflict(
            "Release evidence cannot contain selected credential material or unavailable redaction."
        )
    if not failure.error_summary or not failure.evidence.exists():
        raise RunConflict("Failed scenario lacks persisted useful diagnostics.")
    if not success.plans.exists() or not success.contexts.exists():
        raise RunConflict("Successful scenario lacks saved context and plan.")
    delivery = success.evidence.filter(kind="verified_delivery").order_by("-pk").first()
    validation = (
        delivery.payload.get("implementation", {}).get("validation", [])
        if delivery
        else []
    )
    if delivery is None or not validation:
        raise RunConflict("Successful scenario lacks verified delivery and validation.")
    observations = report.get("pr_observations", [])
    if not isinstance(observations, list) or len(observations) != 2:
        raise RunConflict(
            "Record PR identity before and after an actual controller restart."
        )
    first, second = observations
    expected = {
        "url": success.result_url,
        "commit_sha": delivery.payload.get("commit_sha"),
        "repository_url": entry.repository_url,
    }
    for observation in observations:
        if not isinstance(observation, dict) or any(
            observation.get(key) != value for key, value in expected.items()
        ):
            raise RunConflict("Observed PR does not match persisted delivery evidence.")
        if observation.get("matching_pr_count") != 1 or not observation.get(
            "head_branch"
        ):
            raise RunConflict(
                "Release needs exactly one real PR for the validated task branch."
            )
    if (
        first.get("controller_boot_id") == second.get("controller_boot_id")
        or not first.get("controller_boot_id")
        or not second.get("controller_boot_id")
    ):
        raise RunConflict("PR observations must straddle a controller restart.")
    if first.get("head_branch") != second.get("head_branch"):
        raise RunConflict("Restart changed the delivered branch.")
    verify_model_responses(
        report.get("model_responses", []), accounts(configuration_document())
    )
    fields = {
        field
        for event in entry.evidence.get("ui_changes", [])
        for field in event["fields"]
    }
    if not {"paused", "schedule_mode", "reserve_enabled"}.issubset(fields):
        raise RunConflict(
            "Change pause, schedule, and reserve through the settings UI during qualification."
        )
    gates = entry.evidence.get("gates", [])
    reasons = {reason for gate in gates for reason in gate["reasons"]}
    if not {"paused", "outside_window"}.issubset(reasons) or not any(
        reason.startswith("usage_") for reason in reasons
    ):
        raise RunConflict(
            "Runner evidence has not observed every closed processing gate."
        )
    if not gates or gates[-1].get("allowed") is not True:
        raise RunConflict(
            "The runner must observe safe resume after restoring controls."
        )
    from credentials.models import Credential

    reference = success.configuration_snapshot.get("effective", {}).get(
        "repository_credential_id"
    )
    credential = Credential.objects.filter(
        pk=reference, provider="github", kind="token", revoked_at__isnull=True
    ).first()
    if credential is None:
        raise RunConflict(
            "Select a current repository credential with Actions artifact read permission."
        )
    try:
        workflow = verify_boundary_workflow(boundary, credential.get_secret()["secret"])
    except Exception:
        raise RunConflict(
            "The deployment workflow and downloaded artifact could not be independently verified."
        ) from None
    entry.evidence["verified_workflow"] = workflow
    entry.evidence["live_report"] = report
    entry.qualified_at = timezone.now()
    entry.expires_at = timezone.now() + timedelta(days=7)
    entry.blocker = ""
    entry.save()
    return entry


def verify_model_responses(responses, required_accounts):
    if (
        not isinstance(responses, list)
        or not responses
        or any(
            not isinstance(row, dict)
            or not isinstance(row.get("backend"), str)
            or not isinstance(row.get("model"), str)
            or type(row.get("credential_id")) is not int
            or type(row.get("secret_version")) is not int
            or row.get("backend") not in TRANSPORTS
            or row.get("transport") != TRANSPORTS.get(row.get("backend"))
            for row in responses
        )
    ):
        raise RunConflict(
            "Live backend authentication and structured proposal evidence are required."
        )
    for row in responses:
        if row["backend"] != "opencode":
            if row.get(
                "receipt_kind", "provider_response_id"
            ) != "provider_response_id" or not valid_response_id(
                row.get("response_id")
            ):
                raise RunConflict("Hosted model response identity is missing.")
            continue
        verify_opencode_receipt(row, required_accounts)
    observed = {
        (
            row.get("backend"),
            row.get("credential_id"),
            row.get("model"),
            row.get("secret_version"),
            row.get("transport"),
        )
        for row in responses
    }
    required = {
        (
            row["backend"],
            row["credential_id"],
            row["model"],
            row["secret_version"],
            row["transport"],
        )
        for row in required_accounts
    }
    if not required.issubset(observed):
        raise RunConflict(
            "Not every selected account/model/credential version was exercised live."
        )


def verify_opencode_receipt(row, required_accounts):
    from credentials.models import Credential
    from coding_runner.opencode_connection import validate_connection

    keys = ("backend", "credential_id", "model", "secret_version", "transport")
    if not any(
        all(row.get(key) == account.get(key) for key in keys)
        for account in required_accounts
    ):
        raise RunConflict(
            "OpenCode receipt is outside the selected account/model scope."
        )
    credential = Credential.objects.filter(
        pk=row["credential_id"],
        provider="opencode",
        kind="connection",
        secret_version=row["secret_version"],
        revoked_at__isnull=True,
    ).first()
    if credential is None:
        raise RunConflict("Selected OpenCode connection is unavailable or rotated.")
    try:
        selected = validate_connection(json.loads(credential.get_secret()["secret"]))
    except (ValueError, TypeError, KeyError, ValidationError):
        raise RunConflict("Selected OpenCode connection cannot be verified.") from None
    if (
        row.get("inference_provider") != selected["provider"]
        or row.get("endpoint") != selected["base_url"]
    ):
        raise RunConflict(
            "OpenCode receipt does not match the selected inference connection."
        )
    kind = row.get("receipt_kind")
    if selected["provider"] == "ollama":
        digest = row.get("response_digest")
        if (
            kind != "local_response_digest"
            or not isinstance(digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", digest) is None
            or row.get("response_id") is not None
        ):
            raise RunConflict(
                "Local Ollama requires an explicitly typed raw HTTP response digest."
            )
        version = row.get("ollama_version")
        if version is not None and (
            not isinstance(version, str) or not version or len(version) > 100
        ):
            raise RunConflict("Invalid reported Ollama version.")
        return
    response_id = row.get("response_id")
    if kind != "provider_response_id" or not valid_response_id(response_id):
        raise RunConflict(
            "Hosted OpenCode connections require their provider response identity."
        )
