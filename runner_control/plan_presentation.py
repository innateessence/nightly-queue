from copy import copy

from django import forms
from django.core.exceptions import ValidationError
from django.db import transaction

from credentials.redaction import redact_run_payload, redact_selected_payload
from credentials.references import configuration_credential_ids, snapshot_credential_ids

from .services import redact_payload
from .factory_presentation import factory_summary, publication_summary

REDACTION_UNAVAILABLE = (
    "Selected credential redaction is unavailable; saved content is hidden."
)


@transaction.atomic
def present_payload(run, payload, *, bearer="", preserve_secret_versions=False):
    secrets = [bearer]
    if run.item_id and run.item.claim_token:
        secrets.extend([str(run.item.claim_token), run.item.claim_token.hex])
    try:
        safe = redact_run_payload(
            run, payload, preserve_secret_versions=preserve_secret_versions
        )
        return (
            redact_payload(
                safe, secrets, preserve_secret_versions=preserve_secret_versions
            ),
            "",
        )
    except ValidationError:
        return None, REDACTION_UNAVAILABLE


@transaction.atomic
def present_configuration_payload(
    configuration,
    payload,
    *,
    run=None,
    runs=(),
    secrets=(),
    preserve_secret_versions=False,
):
    references = configuration_credential_ids(configuration)
    selected_runs = [run] if run is not None else []
    secrets = list(secrets)
    for selected in [*selected_runs, *runs]:
        references.update(snapshot_credential_ids(selected.configuration_snapshot))
        if selected.item_id and selected.item.claim_token:
            secrets.extend(
                [str(selected.item.claim_token), selected.item.claim_token.hex]
            )
    try:
        safe = redact_selected_payload(
            references, payload, preserve_secret_versions=preserve_secret_versions
        )
        return (
            redact_payload(
                safe, secrets, preserve_secret_versions=preserve_secret_versions
            ),
            "",
        )
    except ValidationError:
        return None, REDACTION_UNAVAILABLE


def present_run(run):
    safe = copy(run)
    fields = (
        "wait_reason",
        "error_summary",
        "result_url",
        "cancellation_reason",
        "native_status",
        "native_outcome",
    )
    payload = [
        (
            (run.source_snapshot.get("title") or run.item.title)
            if run.item_id
            else "Empty queue"
        ),
        *[getattr(run, field) for field in fields],
        run.runner.name,
        getattr(run, "_factory_documents", []),
        getattr(run, "_publication_observation", None),
        getattr(run, "_factory_authority", {}),
    ]
    data, error = present_payload(run, payload)
    safe.presentation_error = error
    for index, field in enumerate(fields, 1):
        empty = {} if field == "native_status" else ""
        setattr(safe, field, data[index] if data is not None else empty)
    if error:
        safe.wait_reason = error
    if run.item_id:
        safe.item = copy(run.item)
        safe.item.title = data[0] if data is not None else "Saved content unavailable"
    safe.runner = copy(run.runner)
    safe.runner.name = data[len(fields) + 1] if data is not None else "Runner"
    safe.factory = (
        factory_summary(data[len(fields) + 2], data[len(fields) + 4])
        if data is not None
        else {
            "present": bool(getattr(run, "_factory_documents", [])),
            "unavailable": True,
            "ready": False,
        }
    )
    observation = data[len(fields) + 3] if data is not None else None
    safe.publication = publication_summary(
        observation,
        safe.result_url,
        state=safe.state,
        outcome=safe.native_outcome,
        cursor=safe.native_status.get("publication_observation"),
    )
    return safe


def present_evidence(entry):
    safe = copy(entry)
    data, error = present_payload(entry.run, [entry.kind, entry.payload])
    safe.kind = data[0] if data is not None else "Content unavailable"
    safe.payload = data[1] if data is not None else error
    return safe


def present_global_record(entry, configuration, *, field="report", run=None):
    safe = copy(entry)
    data, error = present_configuration_payload(
        configuration, [entry.runner.name, getattr(entry, field)], run=run
    )
    safe.runner = copy(entry.runner)
    safe.runner.name = data[0] if data is not None else "Runner"
    setattr(safe, field, data[1] if data is not None else {})
    safe.presentation_error = error
    return safe


def report_run(evidence):
    from .models import Run

    run_id = evidence.report.get("run_id")
    if not run_id:
        return None
    try:
        return (
            Run.objects.select_related("item")
            .filter(pk=run_id, runner_id=evidence.runner_id)
            .first()
        )
    except (ValidationError, ValueError, TypeError):
        return None


def present_gates(entries, configuration=None):
    from django.utils import timezone
    from django.utils.dateparse import parse_datetime

    result = []
    for entry in entries:
        from .configuration import configuration_document

        current = (
            configuration
            if configuration is not None
            else configuration_document(entry.runner)
        )
        safe = present_global_record(entry, current, run=report_run(entry))
        next_check = parse_datetime(entry.report["next_check_at"])
        safe.stale = next_check is None or next_check <= timezone.now()
        safe.configuration_changed = (
            entry.report["configuration_revision"] != current["revision"]
        )
        result.append(safe)
    return result


def present_configuration(configuration):
    safe, error = present_configuration_payload(
        configuration, configuration, preserve_secret_versions=True
    )
    if safe is not None:
        return safe
    return {
        "execution_ready": False,
        "readiness_reason": error,
        "global": {},
        "projects": [],
    }


def form_presentation_error(form, configuration):
    values = []
    for name, field in form.fields.items():
        bound = form[name]
        if isinstance(field, forms.JSONField):
            raw = bound.data if form.is_bound and not field.disabled else bound.initial
            try:
                values.append(field.to_python(raw))
            except ValidationError:
                return "Invalid JSON content is withheld; reload saved settings to correct the input."
        else:
            values.append(bound.value())
        if isinstance(field, forms.ModelChoiceField):
            values.extend(str(label) for _, label in field.choices)
    safe, error = present_configuration_payload(
        configuration, values, preserve_secret_versions=True
    )
    if error:
        return error
    if safe != values:
        return "Selected credential content is withheld; remove it through the trusted administrator before editing these settings."
    return ""
