import hashlib
import json

from django.core.exceptions import ValidationError
from django.forms.models import model_to_dict
from django.db import transaction

from credentials.models import Credential
from projects.models import Project
from coding_runner.policy import ExecutionPolicy
from coding_runner.native_evidence import native_evidence_contract

from .settings_models import ProjectRunnerSettings, RunnerSettings
from .validators import OVERRIDE_FIELDS, validate_backend_model


SCHEMA_VERSION = 1


def settings_values(saved):
    values = model_to_dict(saved)
    values.pop("id", None)
    for field in ("research", "implementation", "issue", "repository"):
        values[f"{field}_credential_id"] = values.pop(f"{field}_credential")
    return values


def project_configuration(project, defaults, overrides, credentials):
    effective = {field: defaults[field] for field in OVERRIDE_FIELDS}
    sources = {field: "global" for field in OVERRIDE_FIELDS}
    for field, entry in overrides.items():
        mode = entry["mode"]
        if mode == "inherit":
            continue
        value = [] if field.endswith("commands") else ""
        if field.endswith("credential_id"):
            value = None
        effective[field] = entry["value"] if mode == "replace" else value
        sources[field] = mode
    errors = []
    capabilities = [
        "isolated_runtime",
        "native_session",
        "validation_profile",
        "checkpoint",
        "issue_context",
        "git_delivery",
    ]
    for stage in ("implementation",):
        for field in ("backend", "model", "credential_id"):
            key = f"{stage}_{field}"
            if not effective[key]:
                errors.append(
                    {
                        "field": key,
                        "code": "required",
                        "message": "Select a value for this stage.",
                    }
                )
        backend = effective[f"{stage}_backend"]
        if backend:
            capabilities.append(f"backend:{backend}")
        try:
            validate_backend_model(backend, effective[f"{stage}_model"])
        except ValidationError as exc:
            errors.append(
                {
                    "field": f"{stage}_model",
                    "code": "invalid_model",
                    "message": "; ".join(exc.messages),
                }
            )
        reference = effective[f"{stage}_credential_id"]
        if reference and reference in credentials:
            credential = credentials[reference]
            if credential["provider"] != backend:
                errors.append(
                    {
                        "field": f"{stage}_credential_id",
                        "code": "wrong_provider",
                        "message": "Credential provider must match the backend.",
                    }
                )
        if defaults["reserve_enabled"] and backend:
            capabilities.append(f"usage:{backend}")
    for field in ("issue_credential_id", "repository_credential_id"):
        reference = effective[field]
        credential = credentials.get(reference)
        if not reference:
            errors.append(
                {
                    "field": field,
                    "code": "required",
                    "message": "Select a runner GitHub token; import credentials are independent.",
                }
            )
        elif credential and (
            credential["provider"] != "github" or credential["kind"] != "token"
        ):
            errors.append(
                {
                    "field": field,
                    "code": "wrong_provider",
                    "message": "Select a GitHub token credential.",
                }
            )
    for field in OVERRIDE_FIELDS:
        if (
            field.endswith("credential_id")
            and not field.startswith("research_")
            and effective[field]
            and effective[field] not in credentials
        ):
            errors.append(
                {
                    "field": field,
                    "code": "missing_credential",
                    "message": "The selected credential no longer exists.",
                }
            )
    for field in OVERRIDE_FIELDS:
        if not field.endswith("credential_id") or field.startswith("research_"):
            continue
        credential = credentials.get(effective[field])
        if credential and credential.get("revoked_at"):
            errors.append(
                {
                    "field": field,
                    "code": "revoked_credential",
                    "message": "The selected credential is revoked.",
                }
            )
        if not credential or not field.startswith(("research", "implementation")):
            continue
        allowed = {
            "codex": ("token",),
            "claude": ("token",),
            "opencode": ("connection",),
        }
        if credential["kind"] not in allowed.get(credential["provider"], ()):
            errors.append(
                {
                    "field": field,
                    "code": "wrong_kind",
                    "message": "Credential type is unsupported for this backend.",
                }
            )
    if not effective["validation_commands"]:
        errors.append(
            {
                "field": "validation_commands",
                "code": "required",
                "message": "Configure required validation commands.",
            }
        )
    policy = ExecutionPolicy.from_data(effective["execution_policy"])
    return {
        "execution_kind": "native",
        "execution_policy": policy.to_data(),
        "policy_digest": policy.digest,
        "delivery_contract_version": 2,
        "project_id": project.pk,
        "project_name": project.name,
        "repository_url": project.repository_url,
        "base_branch": project.base_branch,
        "autonomous": project.autonomous,
        "source_policy": {
            "repository_id": project.github_repository_id,
            "owner_id": project.github_owner_id,
            "default_branch": project.github_default_branch,
            "permitted_author_ids": project.permitted_author_ids,
            "enabled_at": (
                project.enabled_at.isoformat() if project.enabled_at else None
            ),
            "backfill_issues": project.backfill_issues,
        },
        "effective": effective,
        "sources": sources,
        "errors": errors,
        "structurally_valid": not errors,
        "required_capabilities": sorted(set(capabilities)),
        "execution_ready": False,
    }


def configuration_document():
    with transaction.atomic():
        RunnerSettings.objects.get_or_create(pk=1)
        saved = RunnerSettings.objects.select_for_update().get(pk=1)
        return _configuration_document(saved)


def _configuration_document(saved):
    defaults = settings_values(saved)
    credential_rows = Credential.objects.values(
        "id", "name", "provider", "kind", "updated_at", "revoked_at", "secret_version"
    )
    credentials = {
        row["id"]: {
            **row,
            "updated_at": row["updated_at"].isoformat(),
            "revoked_at": row["revoked_at"].isoformat() if row["revoked_at"] else None,
        }
        for row in credential_rows
    }
    overrides = dict(
        ProjectRunnerSettings.objects.values_list("project_id", "overrides")
    )
    projects = [
        project_configuration(
            project, defaults, overrides.get(project.pk, {}), credentials
        )
        for project in Project.objects.all()
    ]
    selected = set()
    accounts = {}
    for project in projects:
        effective = project["effective"]
        selected.update(
            effective[field]
            for field in OVERRIDE_FIELDS
            if field.endswith("credential_id")
            and not field.startswith("research_")
            and effective[field]
        )
        for stage in ("implementation",):
            backend = effective[f"{stage}_backend"]
            reference = effective[f"{stage}_credential_id"]
            if backend:
                key = (backend, reference)
                account = accounts.setdefault(
                    key,
                    {"backend": backend, "credential_id": reference, "project_ids": []},
                )
                if project["project_id"] not in account["project_ids"]:
                    account["project_ids"].append(project["project_id"])
    # Include default accounts even before a project exists.
    for stage in ("implementation",):
        backend = defaults[f"{stage}_backend"]
        reference = defaults[f"{stage}_credential_id"]
        if backend:
            accounts.setdefault(
                (backend, reference),
                {"backend": backend, "credential_id": reference, "project_ids": []},
            )
        if reference:
            selected.add(reference)
    for field in ("issue_credential_id", "repository_credential_id"):
        if defaults[field]:
            selected.add(defaults[field])
    document = {
        "schema_version": SCHEMA_VERSION,
        "global": defaults,
        "projects": projects,
        "accounts": list(accounts.values()),
        "credentials": [
            credentials[reference]
            for reference in sorted(selected)
            if reference in credentials
        ],
        "execution_ready": False,
        "readiness_reason": "Live execution requires controlled release verification; structural validity grants no execution permission.",
    }
    document["execution_kind"] = "native"
    document["delivery_contract"] = native_evidence_contract()
    document["release_verification"] = {
        "qualified": False,
        "execution_kind": "native",
        "reason": "Native runtime and backend qualification are not installed. Legacy proposal execution is disabled.",
    }
    document["readiness_reason"] = document["release_verification"]["reason"]
    encoded = json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
    document["revision"] = hashlib.sha256(encoded).hexdigest()
    return document


def effective_project_configuration(project):
    document = configuration_document()
    return next(
        entry for entry in document["projects"] if entry["project_id"] == project.pk
    )


def locked_configuration_snapshot(project, expected_revision):
    # The caller owns the claim transaction; configuration writers share this lock.
    RunnerSettings.objects.get_or_create(pk=1)
    list(RunnerSettings.objects.select_for_update().filter(pk=1))
    list(Credential.objects.select_for_update().values_list("pk", flat=True))
    document = configuration_document()
    if expected_revision != document["revision"]:
        raise ValidationError(
            {
                "configuration_revision": "Configuration changed; fetch configuration and repeat preflight."
            }
        )
    effective = next(
        entry for entry in document["projects"] if entry["project_id"] == project.pk
    )
    if effective["errors"]:
        raise ValidationError(
            {"configuration": [error["message"] for error in effective["errors"]]}
        )
    selected = {
        value
        for key, value in effective["effective"].items()
        if key.endswith("credential_id") and value
    }
    snapshot = {
        "credential_references": [
            entry for entry in document["credentials"] if entry["id"] in selected
        ],
        "schema_version": SCHEMA_VERSION,
        "revision": document["revision"],
        "global": document["global"],
        **effective,
    }
    return document["revision"], snapshot
