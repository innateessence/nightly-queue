import json

from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.views.decorators.debug import sensitive_variables

from .models import Credential, CredentialSecretVersion
from .references import snapshot_credential_ids
from .validation import validate_structured_secret


@sensitive_variables()
def credential_values(credential):
    material = credential.get_secret()
    values = [material["secret"], material["passphrase"]]
    if credential.kind in (Credential.Kind.SESSION, Credential.Kind.CONNECTION):
        validate_structured_secret(
            credential.provider, credential.kind, material["secret"]
        )
    if credential.kind == Credential.Kind.SESSION:
        tokens = json.loads(material["secret"])["tokens"]
        values.extend(
            tokens[field] for field in ("access_token", "refresh_token", "id_token")
        )
    elif credential.kind == Credential.Kind.CONNECTION:
        values.append(json.loads(material["secret"]).get("api_key", ""))
    return values


@sensitive_variables()
def selected_credential_values(references):
    from runner_control.settings_models import RunnerSettings

    if not references:
        return []
    RunnerSettings.objects.get_or_create(pk=1)
    RunnerSettings.objects.select_for_update().get(pk=1)
    credentials = list(Credential.objects.select_for_update().filter(pk__in=references))
    if len(credentials) != len(references):
        raise ValidationError(
            {
                "credential": "Selected credential material is unavailable for report redaction."
            }
        )
    values = []
    try:
        for credential in credentials:
            values.extend(credential_values(credential))
        versions = CredentialSecretVersion.objects.filter(credential_id__in=references)
        for version in versions:
            credential = Credential(
                provider=version.provider,
                kind=version.kind,
                encrypted_payload=version.encrypted_payload,
            )
            values.extend(credential_values(credential))
    except (ImproperlyConfigured, ValidationError, ValueError, TypeError, KeyError):
        raise ValidationError(
            {
                "credential": "Unable to redact selected credentials; check VIBEARR_CREDENTIAL_KEY and credential material."
            }
        ) from None
    return sorted({value for value in values if value}, key=len, reverse=True)


@sensitive_variables()
def redact_selected_payload(references, payload, *, preserve_secret_versions=False):
    from runner_control.services import redact_payload

    values = selected_credential_values(references)
    return redact_payload(
        payload, values, preserve_secret_versions=preserve_secret_versions
    )


@sensitive_variables()
def redact_run_payload(run, payload, *, preserve_secret_versions=False):
    """Call in a transaction after run authorization; mutations also require an active claim."""
    references = snapshot_credential_ids(run.configuration_snapshot)
    if not references:
        return payload
    return redact_selected_payload(
        references, payload, preserve_secret_versions=preserve_secret_versions
    )
