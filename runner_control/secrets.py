from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.db import transaction
from django.http import JsonResponse
from django.views.decorators.cache import never_cache
from django.views.decorators.debug import sensitive_variables

from credentials.models import Credential
from nightly_queue.api import read_body, string_field

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
        if set(data) != {"scope", "backend", "credential_id"}:
            unavailable("Preflight requires a configured backend account reference.")
        reference = data["credential_id"]
        backend = data["backend"]
        selected = any(
            account["backend"] == backend and account["credential_id"] == reference
            for account in document["accounts"]
        )
        if type(reference) is not int or not selected:
            unavailable("This account is not selected for runner preflight.")
        return reference, backend
    if scope != "run" or set(data) != {"scope", "run_id", "claim_token", "purpose"}:
        unavailable("Use a configured preflight account or an owned active run.")
    run = owned_run(runner, uuid_field(data, "run_id"))
    verify_active(run, uuid_field(data, "claim_token"))
    purpose = data["purpose"]
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
                "Credential encryption is unavailable; configure NQ_CREDENTIAL_KEY."
            )
        except ValidationError:
            unavailable(
                "Unable to decrypt or validate the selected credential; replace it or check NQ_CREDENTIAL_KEY."
            )
        return {
            "schema_version": 1,
            "credential": {
                "id": credential.pk,
                "provider": credential.provider,
                "kind": credential.kind,
                "version": credential.secret_version,
            },
            "material": material,
        }


@never_cache
@runner_api_view("POST")
@sensitive_variables()
def selected_secret(request):
    data = read_body(
        request,
        ["scope", "backend", "credential_id", "run_id", "claim_token", "purpose"],
    )
    try:
        return JsonResponse(secret_document(data, request.runner_identity))
    except RunNotFound:
        return JsonResponse({"error": "Run not found."}, status=404)
    except RunConflict as exc:
        return JsonResponse({"error": str(exc)}, status=409)
