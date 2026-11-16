from copy import copy

from django.core.exceptions import ValidationError
from django.db import transaction

from credentials.redaction import redact_run_payload

from .services import redact_payload

REDACTION_UNAVAILABLE = (
    "Selected credential redaction is unavailable; saved content is hidden."
)


@transaction.atomic
def present_payload(run, payload, *, bearer=""):
    secrets = [bearer]
    if run.item_id and run.item.claim_token:
        secrets.extend([str(run.item.claim_token), run.item.claim_token.hex])
    try:
        safe = redact_run_payload(run, payload)
        return redact_payload(safe, secrets), ""
    except ValidationError:
        return None, REDACTION_UNAVAILABLE


def present_run(run):
    safe = copy(run)
    payload = [run.item.title if run.item_id else "Empty queue", run.wait_reason]
    data, error = present_payload(run, payload)
    safe.presentation_error = error
    safe.wait_reason = data[1] if data is not None else error
    if run.item_id:
        safe.item = copy(run.item)
        safe.item.title = data[0] if data is not None else "Saved content unavailable"
    return safe
