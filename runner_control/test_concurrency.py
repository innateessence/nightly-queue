from .test_support import CONTROLLER_SECRET
import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import patch

from django.db import connection, connections
from django.test import Client, TransactionTestCase
from django.urls import reverse

from projects.models import Project
from work_queue.models import QueueItem
from work_queue.services import enqueue_item, requeue_items

from .auth import token_digest
from .models import Run, RunEvidence, RunnerController, RunnerIdentity


class RunConcurrencyTests(TransactionTestCase):
    def setUp(self):
        snapshot = patch(
            "runner_control.services.locked_configuration_snapshot",
            return_value=("", {}),
        )
        snapshot.start()
        self.addCleanup(snapshot.stop)
        self.token = "runner-concurrency-token-" + "a" * 32
        self.runner = RunnerIdentity.objects.create(
            name="controller",
            token_digest=token_digest(self.token),
            controller_digest=token_digest(CONTROLLER_SECRET),
        )
        self.project = Project.objects.create(
            name="Example", repository_url="https://github.com/example/repo"
        )
        self.first = enqueue_item(project=self.project, title="First")
        self.second = enqueue_item(project=self.project, title="Second")
        RunnerController.objects.get_or_create(pk=1)

    def post(self, name, data, barrier=None, run_id=None):
        try:
            with connection.cursor() as cursor:
                cursor.execute("SET statement_timeout = '5s'")
            client = Client(
                HTTP_AUTHORIZATION=f"Bearer {self.token}",
                HTTP_X_NQ_CONTROLLER_SECRET=CONTROLLER_SECRET,
            )
            kwargs = {"run_id": run_id} if run_id else {}
            if barrier:
                barrier.wait(timeout=5)
            response = client.post(
                reverse(name, kwargs=kwargs),
                json.dumps(data),
                content_type="application/json",
            )
            return response.status_code, response.json()
        finally:
            connections.close_all()

    def test_concurrent_duplicate_claims_return_same_item_and_token(self):
        run_id = uuid.uuid4()
        barrier = Barrier(2)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(self.post, "runner_claim", {"run_id": str(run_id)}, barrier)
                for _ in range(2)
            ]
            responses = [future.result(timeout=10) for future in futures]
        self.assertEqual([status for status, _ in responses], [200, 200])
        items = [data["item"] for _, data in responses]
        self.assertEqual(items[0], items[1])
        self.assertEqual(Run.objects.count(), 1)
        self.assertEqual(QueueItem.objects.filter(status="running").count(), 1)
        self.assertEqual(QueueItem.objects.filter(status="queued").count(), 1)

    def test_concurrent_different_claims_cannot_overlap_controller(self):
        barrier = Barrier(2)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(
                    self.post, "runner_claim", {"run_id": str(uuid.uuid4())}, barrier
                )
                for _ in range(2)
            ]
            responses = [future.result(timeout=10) for future in futures]
        self.assertCountEqual([status for status, _ in responses], [200, 409])
        self.assertEqual(Run.objects.filter(state="active").count(), 1)

    def test_concurrent_duplicate_evidence_uploads_append_once(self):
        run_id = uuid.uuid4()
        _, claim = self.post("runner_claim", {"run_id": str(run_id)})
        fields = {
            "operation_id": str(uuid.uuid4()),
            "claim_token": claim["item"]["claim_token"],
            "kind": "log",
            "data": {"message": "Saved"},
        }
        barrier = Barrier(2)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(self.post, "runner_artifact", fields, barrier, run_id)
                for _ in range(2)
            ]
            responses = [future.result(timeout=10) for future in futures]
        self.assertEqual([status for status, _ in responses], [200, 200])
        self.assertEqual(responses[0][1], responses[1][1])
        self.assertEqual(RunEvidence.objects.count(), 1)

    def test_concurrent_duplicate_finish_requests_reconcile(self):
        run_id = uuid.uuid4()
        _, claim = self.post("runner_claim", {"run_id": str(run_id)})
        fields = {
            "claim_token": claim["item"]["claim_token"],
            "status": "failed",
            "error_summary": "Stopped",
        }
        barrier = Barrier(2)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(self.post, "runner_finish", fields, barrier, run_id)
                for _ in range(2)
            ]
            responses = [future.result(timeout=10) for future in futures]
        self.assertEqual([status for status, _ in responses], [200, 200])
        self.assertEqual(responses[0][1], responses[1][1])
        self.assertEqual(Run.objects.get(pk=run_id).state, "completed")

    def requeue(self, item_id, barrier):
        try:
            with connection.cursor() as cursor:
                cursor.execute("SET statement_timeout = '5s'")
            barrier.wait(timeout=5)
            return requeue_items(QueueItem.objects.filter(pk=item_id))
        finally:
            connections.close_all()

    def test_requeue_racing_progress_never_accepts_obsolete_ownership(self):
        run_id = uuid.uuid4()
        _, claim = self.post("runner_claim", {"run_id": str(run_id)})
        fields = {
            "operation_id": str(uuid.uuid4()),
            "claim_token": claim["item"]["claim_token"],
            "stage": "research",
        }
        barrier = Barrier(2)
        with ThreadPoolExecutor(max_workers=2) as pool:
            progress = pool.submit(
                self.post, "runner_progress", fields, barrier, run_id
            )
            requeue = pool.submit(self.requeue, claim["item"]["id"], barrier)
            status, _ = progress.result(timeout=10)
            self.assertEqual(requeue.result(timeout=10), 1)
        self.assertIn(status, [200, 409])
        self.assertEqual(Run.objects.get(pk=run_id).state, "obsolete")
        self.assertEqual(self.post("runner_progress", fields, run_id=run_id)[0], 409)
        self.assertEqual(RunEvidence.objects.count(), 1 if status == 200 else 0)
