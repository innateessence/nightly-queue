from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.db import transaction
from django.http import JsonResponse
from django.views.decorators.cache import never_cache
from django.views.decorators.debug import sensitive_variables

from credentials.models import Credential, RunnerPreflightGrant
from vibearr_app.api import read_body, string_field

from .auth import runner_api_view
from .configuration import configuration_document
from .services import (
    RunConflict,
    RunNotFound,
    lock_controller,
    owned_run,
    uuid_field,
    verify_active,
)
from .settings_models import RunnerSettings

PURPOSES = ("research", "implementation", "issue", "repository")


def unavailable(message="The selected credential is unavailable."):
    raise ValidationError({"credential": message})


def selected_reference(data, runner, document):
    scope = string_field(data, "scope", max_length=20)
    if scope == "preflight":
        github = data.get("backend") == "github"
        expected = {"scope", "backend", "credential_id"} | (
            {"project_id"} if github else set()
        )
        if set(data) != expected:
            unavailable("Preflight requires a configured backend account reference.")
        reference = data["credential_id"]
        backend = data["backend"]
        if type(reference) is not int or reference < 1:
            unavailable("Preflight requires a positive selected credential ID.")
        if github:
            project_id = data["project_id"]
            selected = type(project_id) is int and any(
                project["project_id"] == project_id
                and project["execution_kind"] == "native"
                and reference
                in {
                    project["effective"].get("issue_credential_id"),
                    project["effective"].get("repository_credential_id"),
                }
                for project in document["projects"]
            )
        else:
            selected = any(
                account["backend"] == backend and account["credential_id"] == reference
                for account in document["accounts"]
            )
        granted = (
            RunnerPreflightGrant.objects.select_for_update()
            .filter(runner=runner, credential_id=reference)
            .exists()
            if type(reference) is int
            else False
        )
        if type(reference) is not int or not selected or not granted:
            unavailable("This account is not selected for runner preflight.")
        return reference, backend
    if scope not in {"run", "reconciliation", "completion"} or set(data) != {
        "scope",
        "run_id",
        "claim_token",
        "purpose",
    }:
        unavailable("Use a configured preflight account or an owned active run.")
    run = owned_run(runner, uuid_field(data, "run_id"))
    reconciliation = scope == "reconciliation"
    completion = scope == "completion"
    verify_active(
        run,
        uuid_field(data, "claim_token"),
        allow_cancellation=reconciliation or completion,
    )
    purpose = string_field(data, "purpose", max_length=30)
    if hasattr(run, "delivery_recovery") and purpose not in {"issue", "repository"}:
        unavailable("Delivery-only recovery cannot obtain coding credentials.")
    if completion:
        from .merge_services import completion_authority

        if purpose not in {"issue", "repository"}:
            unavailable("Completion credentials are restricted to GitHub reporting.")
        completion_authority(run, document)
    if reconciliation:
        from .native_services import registered

        if (
            run.configuration_snapshot.get("execution_kind") != "native"
            or not registered(run)
            or purpose not in {"issue", "repository"}
        ):
            unavailable(
                "Reconciliation credentials require registered owned native work and a GitHub read purpose."
            )
    if (
        run.configuration_snapshot.get("execution_kind") == "native"
        and purpose == "research"
    ):
        unavailable("Separate research credentials are inactive for native execution.")
    if purpose not in PURPOSES:
        unavailable("Choose a supported credential purpose.")
    effective = run.configuration_snapshot.get("effective", {})
    reference = effective.get(f"{purpose}_credential_id")
    project = next(
        (
            entry
            for entry in document["projects"]
            if entry["project_id"] == run.item.project_id
        ),
        None,
    )
    if (
        not project
        or not reference
        or project["effective"].get(f"{purpose}_credential_id") != reference
    ):
        unavailable(
            "The run credential selection changed; stop and reconcile configuration."
        )
    backend = (
        effective.get(f"{purpose}_backend")
        if purpose in ("research", "implementation")
        else "github"
    )
    if (
        purpose in ("research", "implementation")
        and project["effective"].get(f"{purpose}_backend") != backend
    ):
        unavailable(
            "The run backend selection changed; stop and reconcile configuration."
        )
    return reference, backend


@sensitive_variables()
def secret_document(data, runner):
    with transaction.atomic():
        lock_controller(runner)
        RunnerSettings.objects.get_or_create(pk=1)
        RunnerSettings.objects.select_for_update().get(pk=1)
        document = configuration_document()
        reference, backend = selected_reference(data, runner, document)
        credential = Credential.objects.select_for_update().filter(pk=reference).first()
        if not credential or credential.revoked_at:
            unavailable("The selected credential is missing or revoked.")
        if credential.provider != backend:
            unavailable("The selected credential provider does not match its purpose.")
        allowed = {
            "github": ("token",),
            "codex": ("token", "session"),
            "claude": ("token",),
            "opencode": ("connection",),
        }
        if credential.kind not in allowed.get(backend, ()):
            unavailable("The selected credential type is unsupported for its purpose.")
        try:
            material = credential.get_secret()
            credential.validate_provider_kind()
            if credential.kind in (Credential.Kind.SESSION, Credential.Kind.CONNECTION):
                from credentials.validation import validate_structured_secret

                validate_structured_secret(
                    credential.provider, credential.kind, material["secret"]
                )
        except ImproperlyConfigured:
            unavailable(
                "Credential encryption is unavailable; configure VIBEARR_CREDENTIAL_KEY."
            )
        except ValidationError:
            unavailable(
                "Unable to decrypt or validate the selected credential; replace it or check VIBEARR_CREDENTIAL_KEY."
            )
        result = {
            "schema_version": 1,
            "credential": {
                "id": credential.pk,
                "provider": credential.provider,
                "kind": credential.kind,
                "version": credential.secret_version,
            },
            "material": material,
        }
        if data["scope"] == "reconciliation":
            result["access_mode"] = "reconciliation-read-only"
        elif data["scope"] == "completion":
            result["access_mode"] = "completion-reporting-only"
        elif data["scope"] == "preflight" and data["backend"] == "github":
            result["access_mode"] = "preflight-read-only"
        return result


@never_cache
@runner_api_view("POST")
@sensitive_variables()
def selected_secret(request):
    data = read_body(
        request,
        [
            "scope",
            "backend",
            "credential_id",
            "project_id",
            "run_id",
            "claim_token",
            "purpose",
        ],
    )
    try:
        return JsonResponse(secret_document(data, request.runner_identity))
    except RunNotFound:
        return JsonResponse({"error": "Run not found."}, status=404)
    except RunConflict as exc:
        return JsonResponse({"error": str(exc)}, status=409)
