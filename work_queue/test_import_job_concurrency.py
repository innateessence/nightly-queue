from concurrent.futures import ThreadPoolExecutor
import threading

from django.db import close_old_connections
from django.test import TransactionTestCase

from .import_jobs import claim_import, enqueue_import
from .models import ImportJob


class ImportJobConcurrencyTests(TransactionTestCase):
    def test_concurrent_claims_cannot_own_the_same_job(self):
        queued = enqueue_import()
        barrier = threading.Barrier(2)

        def claim():
            close_old_connections()
            try:
                barrier.wait(timeout=5)
                job = claim_import()
                return job.pk if job else None
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _index: claim(), range(2)))
        self.assertCountEqual(results, [queued.pk, None])
        self.assertEqual(ImportJob.objects.filter(status="running").count(), 1)

    def test_concurrent_clicks_create_one_pending_import(self):
        barrier = threading.Barrier(2)

        def enqueue():
            close_old_connections()
            try:
                barrier.wait(timeout=5)
                return enqueue_import().pk
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _index: enqueue(), range(2)))
        self.assertEqual(results[0], results[1])
        self.assertEqual(ImportJob.objects.count(), 1)
