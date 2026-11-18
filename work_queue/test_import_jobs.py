from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from .import_jobs import (
    claim_import,
    enqueue_import,
    finish_import,
    renew_import,
    run_import,
)
from .models import ImportJob


class ImportJobTests(TestCase):
    def test_repeated_manual_requests_share_pending_job(self):
        self.assertEqual(enqueue_import().pk, enqueue_import().pk)

    def test_manual_request_during_execution_is_retained(self):
        enqueue_import()
        active = claim_import()
        queued = enqueue_import()
        self.assertNotEqual(active.pk, queued.pk)

    def test_claim_does_not_take_running_job(self):
        enqueue_import()
        self.assertIsNotNone(claim_import())
        self.assertIsNone(claim_import())

    def test_expired_lease_is_requeued(self):
        enqueue_import()
        old = claim_import()
        ImportJob.objects.filter(pk=old.pk).update(
            lease_until=timezone.now() - timedelta(seconds=1)
        )
        replacement = claim_import()
        old.refresh_from_db()
        self.assertEqual(old.status, ImportJob.Status.FAILED)
        self.assertNotEqual(old.pk, replacement.pk)

    def test_expired_owner_cannot_report_success(self):
        enqueue_import()
        job = claim_import()
        ImportJob.objects.filter(pk=job.pk).update(
            lease_until=timezone.now() - timedelta(seconds=1)
        )
        self.assertFalse(finish_import(job, {"created": 1}))

    def test_changed_lease_cannot_be_renewed(self):
        enqueue_import()
        job = claim_import()
        ImportJob.objects.filter(pk=job.pk).update(lease_token=None)
        self.assertFalse(renew_import(job))

    @patch("work_queue.import_jobs.import_auto_issues")
    def test_manual_job_bypasses_scheduled_interval(self, importer):
        importer.return_value = {"projects": 1, "created": 2}
        enqueue_import()
        job = claim_import()
        run_import(job)
        self.assertEqual(importer.call_count, 1)
        self.assertFalse(importer.call_args.kwargs["scheduled"])
        job.refresh_from_db()
        self.assertEqual(job.summary, {"projects": 1, "created": 2})
        self.assertEqual(job.status, ImportJob.Status.SUCCEEDED)

    @patch(
        "work_queue.import_jobs.import_auto_issues", side_effect=RuntimeError("private")
    )
    def test_failure_retries_with_backoff_without_private_error(self, _importer):
        enqueue_import()
        job = claim_import()
        run_import(job)
        job.refresh_from_db()
        self.assertEqual(job.summary, {"error": "Issue import failed."})
        self.assertIsNone(claim_import())
        self.assertEqual(ImportJob.objects.filter(status="queued").count(), 1)
