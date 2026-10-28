from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from django.db import connection, connections
from django.test import TransactionTestCase

from projects.models import Project

from .models import QueueItem
from .services import claim_item, claim_next, enqueue_item


class ClaimConcurrencyTests(TransactionTestCase):
    def setUp(self):
        self.project = Project.objects.create(
            name="Example", repository_url="https://github.com/example/repo"
        )
        self.item = enqueue_item(project=self.project, title="First")

    def claim(self, barrier, item_id=None):
        try:
            with connection.cursor() as cursor:
                cursor.execute("SET statement_timeout = '5s'")
            candidate = None
            if item_id is not None:
                candidate = QueueItem.objects.select_related("project").get(pk=item_id)
            barrier.wait(timeout=5)
            item = claim_next() if candidate is None else claim_item(candidate)
            return item.pk if item else None
        finally:
            connections.close_all()

    def test_only_one_worker_can_claim_the_same_candidate(self):
        barrier = Barrier(2)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(self.claim, barrier, self.item.pk) for _ in range(2)]
            claimed_ids = [future.result(timeout=10) for future in futures]
        self.assertCountEqual(claimed_ids, [self.item.pk, None])

    def test_competing_workers_claim_different_items(self):
        second = enqueue_item(project=self.project, title="Second")
        barrier = Barrier(2)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(self.claim, barrier) for _ in range(2)]
            claimed_ids = [future.result(timeout=10) for future in futures]
        self.assertCountEqual(claimed_ids, [self.item.pk, second.pk])
