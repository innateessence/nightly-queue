from django.db import transaction
from django.utils import timezone

from .models import Run
from .services import RunConflict, RunNotFound, lock_controller


@transaction.atomic
def request_cancellation(run_id, reason="Operator requested cancellation"):
    lock_controller()
    try:
        run = Run.objects.get(pk=run_id)
    except Run.DoesNotExist:
        raise RunNotFound("Run not found.") from None
    if run.state != Run.State.ACTIVE:
        raise RunConflict("Only active execution can be cancelled.")
    if not isinstance(reason, str) or len(reason) > 2000:
        raise RunConflict("Cancellation reason must be a bounded string.")
    if run.cancellation_requested_at is None:
        run.cancellation_requested_at = timezone.now()
        run.cancellation_reason = reason
        run.save(
            update_fields=[
                "cancellation_requested_at",
                "cancellation_reason",
                "updated_at",
            ]
        )
    return run
