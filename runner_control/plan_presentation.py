from copy import copy

from django.core.exceptions import ValidationError
from django.db import transaction

from credentials.redaction import redact_run_payload, redact_selected_payload
from credentials.references import configuration_credential_ids, snapshot_credential_ids

from .services import redact_payload

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
    fields = ("wait_reason", "error_summary", "result_url", "cancellation_reason")
    payload = [
        run.item.title if run.item_id else "Empty queue",
        *[getattr(run, field) for field in fields],
    ]
    data, error = present_payload(run, payload)
    safe.presentation_error = error
    for index, field in enumerate(fields, 1):
        setattr(safe, field, data[index] if data is not None else "")
    if error:
        safe.wait_reason = error
    if run.item_id:
        safe.item = copy(run.item)
        safe.item.title = data[0] if data is not None else "Saved content unavailable"
    safe.runner = copy(run.runner)
    name, error = present_payload(run, run.runner.name)
    safe.runner.name = name if name is not None else "Runner"
    return safe


def present_evidence(entry):
    safe = copy(entry)
    data, error = present_payload(entry.run, [entry.kind, entry.payload])
    safe.kind = data[0] if data is not None else "Content unavailable"
    safe.payload = data[1] if data is not None else error
    return safe


def present_approval(approval):
    from .approvals import approval_data

    try:
        with transaction.atomic():
            data = approval_data(approval)
        safe, error = present_payload(approval.run, data)
        return safe if safe is not None else {"decision_display": error}
    except ValidationError:
        return {"decision_display": REDACTION_UNAVAILABLE}


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


def present_gates(entries, configuration):
    from django.utils import timezone
    from django.utils.dateparse import parse_datetime

    result = []
    for entry in entries:
        safe = present_global_record(entry, configuration, run=report_run(entry))
        next_check = parse_datetime(entry.report["next_check_at"])
        safe.stale = next_check is None or next_check <= timezone.now()
        safe.configuration_changed = (
            entry.report["configuration_revision"] != configuration["revision"]
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


def qualification_runs(entry):
    from .models import Run

    report = entry.evidence.get("live_report", {})
    references = [report.get("success_run_id"), report.get("failure_run_id")]
    references.extend(event.get("run_id") for event in entry.evidence.get("gates", []))
    runs = []
    for reference in set(value for value in references if isinstance(value, str)):
        try:
            run = (
                Run.objects.select_related("item")
                .filter(pk=reference, runner_id=entry.runner_id)
                .first()
            )
        except (ValidationError, ValueError, TypeError):
            continue
        if run is not None:
            runs.append(run)
    return runs


def present_release(entry, configuration):
    safe = copy(entry)
    fields = ("repository_url", "blocker", "authorization_note")
    payload = [getattr(entry, field) for field in fields]
    data, error = present_configuration_payload(
        configuration, payload, runs=qualification_runs(entry)
    )
    for index, field in enumerate(fields):
        setattr(safe, field, data[index] if data is not None else error)
    return safe
