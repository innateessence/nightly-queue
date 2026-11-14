import uuid

from django.test import Client, TestCase, override_settings
from django.urls import reverse

from projects.models import Project

from .models import QueueItem
from .services import claim_next, enqueue_item, finish_item, requeue_items


@override_settings(NQ_API_TOKEN="runner-test-token")
class RunnerRunAPITests(TestCase):
    def setUp(self):
        self.run_id = uuid.uuid4()
        self.url = reverse("api_runner_run", args=[self.run_id])
        self.client.defaults["HTTP_AUTHORIZATION"] = "Bearer runner-test-token"
        self.project = Project.objects.create(
            name="Example", repository_url="https://github.com/example/repo"
        )

    def claim(self):
        enqueue_item(project=self.project, title="Implement task")
        return claim_next(execution_ref=f"runner:{self.run_id}")

    def test_lookup_requires_authentication(self):
        self.assertEqual(Client().get(self.url).status_code, 401)

    def test_lookup_only_accepts_get(self):
        self.assertEqual(self.client.post(self.url).status_code, 405)

    def test_unknown_run_returns_null(self):
        self.assertIsNone(self.client.get(self.url).json()["item"])

    def test_lookup_recovers_claim_token_and_checkout(self):
        item = self.claim()
        response = self.client.get(self.url)
        data = response.json()["item"]
        self.assertEqual(data["id"], item.pk)
        self.assertEqual(data["claim_token"], str(item.claim_token))
        self.assertEqual(data["repository_url"], self.project.repository_url)

    def test_other_execution_is_not_returned(self):
        self.claim()
        url = reverse("api_runner_run", args=[uuid.uuid4()])
        self.assertIsNone(self.client.get(url).json()["item"])

    def test_completed_run_returns_result_without_claim_token(self):
        item = self.claim()
        finish_item(
            item_id=item.pk,
            claim_token=item.claim_token,
            status="succeeded",
            result_url="https://github.com/example/repo/pull/1",
        )
        data = self.client.get(self.url).json()["item"]
        self.assertEqual(data["status"], "succeeded")
        self.assertEqual(data["result_url"], "https://github.com/example/repo/pull/1")
        self.assertNotIn("claim_token", data)

    def test_requeue_invalidates_run_lookup(self):
        self.claim()
        requeue_items(QueueItem.objects.all())
        self.assertIsNone(self.client.get(self.url).json()["item"])

    def test_duplicate_execution_reference_returns_conflict(self):
        self.claim()
        self.claim()
        self.assertEqual(self.client.get(self.url).status_code, 409)
