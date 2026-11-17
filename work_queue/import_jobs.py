"""PostgreSQL-backed issue import jobs shared by UI and scheduler."""

import logging
import threading
import time
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

from django.db import close_old_connections, transaction
from django.utils import timezone

from credentials.models import APISettings
from .models import ImportJob
from .tasks import import_auto_issues


logger = logging.getLogger(__name__)
LEASE_SECONDS = 120


def enqueue_import(*, scheduled=False):
    job, _created = ImportJob.objects.get_or_create(
        scheduled=scheduled,
        status=ImportJob.Status.QUEUED,
        defaults={"available_at": timezone.now()},
    )
    return job


@transaction.atomic
def claim_import():
    now = timezone.now()
    expired = ImportJob.objects.select_for_update(skip_locked=True).filter(
        status=ImportJob.Status.RUNNING,
        lease_until__lte=now,
    )
    for job in expired:
        job.status = ImportJob.Status.FAILED
        job.finished_at = now
        job.summary = {"error": "Import lease expired; retry queued."}
        job.save(update_fields=["status", "finished_at", "summary"])
        enqueue_import(scheduled=job.scheduled)
    now = timezone.now()
    job = (
        ImportJob.objects.select_for_update(skip_locked=True)
        .filter(
            status=ImportJob.Status.QUEUED,
            available_at__lte=now,
        )
        .order_by("available_at", "pk")
        .first()
    )
    if job is None:
        return None
    job.status = ImportJob.Status.RUNNING
    job.lease_token = uuid4()
    job.lease_until = now + timedelta(seconds=LEASE_SECONDS)
    job.attempts += 1
    job.save(update_fields=["status", "lease_token", "lease_until", "attempts"])
    return job


def renew_import(job):
    return (
        ImportJob.objects.filter(
            pk=job.pk,
            status=ImportJob.Status.RUNNING,
            lease_token=job.lease_token,
            lease_until__gt=timezone.now(),
        ).update(lease_until=timezone.now() + timedelta(seconds=LEASE_SECONDS))
        == 1
    )


def finish_import(job, summary, *, failed=False):
    return (
        ImportJob.objects.filter(
            pk=job.pk,
            status=ImportJob.Status.RUNNING,
            lease_token=job.lease_token,
            lease_until__gt=timezone.now(),
        ).update(
            status=ImportJob.Status.FAILED if failed else ImportJob.Status.SUCCEEDED,
            summary=summary,
            finished_at=timezone.now(),
            lease_until=None,
        )
        == 1
    )


def run_import(job):
    stopped = threading.Event()

    def heartbeat():
        try:
            while not stopped.wait(LEASE_SECONDS / 3):
                close_old_connections()
                if not renew_import(job):
                    return
                Path("/tmp/vibearr-scheduler-heartbeat").write_text(str(time.time()))
        finally:
            close_old_connections()

    thread = threading.Thread(target=heartbeat, daemon=True)
    thread.start()
    try:

        def checkpoint():
            if not ImportJob.objects.filter(
                pk=job.pk,
                status="running",
                lease_token=job.lease_token,
                lease_until__gt=timezone.now(),
            ).exists():
                raise RuntimeError("Issue import ownership expired.")

        summary = import_auto_issues(scheduled=job.scheduled, checkpoint=checkpoint)
        finish_import(job, summary)
    except Exception:
        logger.warning("Issue import failed; retry queued.", exc_info=False)
        finish_import(job, {"error": "Issue import failed."}, failed=True)
        retry = enqueue_import(scheduled=job.scheduled)
        ImportJob.objects.filter(pk=retry.pk).update(
            available_at=timezone.now() + timedelta(seconds=60)
        )
    finally:
        stopped.set()
        thread.join(timeout=5)


def tick():
    settings = APISettings.objects.filter(pk=1).first()
    if settings and settings.github_credential_id:
        cutoff = timezone.now() - timedelta(seconds=settings.auto_sync_interval_seconds)
        if settings.last_auto_sync_at is None or settings.last_auto_sync_at <= cutoff:
            enqueue_import(scheduled=True)
    job = claim_import()
    if job is not None:
        run_import(job)
