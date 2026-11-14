import json
import uuid

from django.test import Client, TestCase, override_settings
from django.urls import reverse

from projects.models import Project

from .models import QueueItem, RunnerLog
from .services import claim_next, enqueue_item, finish_item, requeue_items


@override_settings(NQ_API_TOKEN="web-log-token")
class RunnerLogTests(TestCase):
    def setUp(self):
        self.project = Project.objects.create(
            name="Example", repository_url="https://github.com/example/repo"
        )
        self.run_id = uuid.uuid4()
        enqueue_item(project=self.project, title="Implement task")
        self.item = claim_next(execution_ref=f"runner:{self.run_id}")
        self.url = reverse("api_runner_logs", args=[self.item.pk])
        self.page = reverse("item_logs", args=[self.item.pk])
        self.feed = reverse("item_log_data", args=[self.item.pk])
        self.client.defaults["HTTP_AUTHORIZATION"] = "Bearer web-log-token"
        self.data = {
            "run_id": str(self.run_id),
            "claim_token": str(self.item.claim_token),
            "sequence": 1,
            "text": "[agent]\nImplementing task\n",
        }

    def upload(self, **changes):
        return self.client.post(
            self.url,
            json.dumps(dict(self.data, **changes)),
            content_type="application/json",
        )

    def test_upload_requires_authentication(self):
        response = Client().post(
            self.url, json.dumps(self.data), content_type="application/json"
        )
        self.assertEqual(response.status_code, 401)
        self.assertFalse(RunnerLog.objects.exists())

    def test_upload_requires_post(self):
        self.assertEqual(self.client.get(self.url).status_code, 405)

    def test_log_is_stored_for_the_claimed_execution(self):
        self.assertEqual(self.upload().status_code, 200)
        entry = RunnerLog.objects.get()
        self.assertEqual(entry.item, self.item)
        self.assertEqual(entry.run_id, self.run_id)
        self.assertEqual(entry.text, self.data["text"])

    def test_duplicate_upload_is_idempotent(self):
        self.upload()
        self.assertEqual(self.upload().status_code, 200)
        self.assertEqual(RunnerLog.objects.count(), 1)

    def test_sequence_cannot_overwrite_previous_output(self):
        self.upload()
        self.assertEqual(self.upload(text="Replacement").status_code, 409)
        self.assertEqual(RunnerLog.objects.get().text, self.data["text"])

    def test_stale_claim_is_rejected(self):
        self.assertEqual(self.upload(claim_token=str(uuid.uuid4())).status_code, 409)
        self.assertFalse(RunnerLog.objects.exists())

    def test_wrong_run_is_rejected(self):
        self.assertEqual(self.upload(run_id=str(uuid.uuid4())).status_code, 409)

    def test_requeued_execution_cannot_upload(self):
        requeue_items(QueueItem.objects.all())
        self.assertEqual(self.upload().status_code, 409)

    def test_completed_execution_cannot_upload(self):
        finish_item(
            item_id=self.item.pk, claim_token=self.item.claim_token, status="succeeded"
        )
        self.assertEqual(self.upload().status_code, 409)

    def test_missing_item_returns_not_found(self):
        self.url = reverse("api_runner_logs", args=[99999])
        self.assertEqual(self.upload().status_code, 404)

    def test_upload_has_bounded_text_and_sequence(self):
        self.assertEqual(self.upload(text="x" * 4097).status_code, 400)
        self.assertEqual(self.upload(sequence=257).status_code, 400)
        self.assertEqual(self.upload(sequence=True).status_code, 400)
        self.assertFalse(RunnerLog.objects.exists())

    def test_api_and_claim_tokens_are_redacted_on_server(self):
        self.upload(text=f"web-log-token {self.item.claim_token}")
        text = RunnerLog.objects.get().text
        self.assertNotIn("web-log-token", text)
        self.assertNotIn(str(self.item.claim_token), text)

    def test_control_sequences_are_removed(self):
        self.upload(text="\x1b[31mError\x1b[0m\0")
        self.assertEqual(RunnerLog.objects.get().text, "Error")

    def test_log_page_escapes_untrusted_output(self):
        self.upload(text="<script>alert('untrusted')</script>")
        response = self.client.get(self.page)
        self.assertContains(response, "&lt;script&gt;")
        self.assertNotContains(response, "<script>alert")
        self.assertContains(response, 'data-running="true"')
        self.assertNotContains(response, str(self.item.claim_token))

    def test_feed_only_returns_events_after_cursor(self):
        self.upload()
        self.upload(sequence=2, text="Second chunk")
        data = self.client.get(self.feed, {"run": str(self.run_id), "after": 1}).json()
        self.assertEqual(data["events"], [{"sequence": 2, "text": "Second chunk"}])
        self.assertTrue(data["running"])

    def test_feed_stops_refreshing_after_completion(self):
        self.upload()
        finish_item(
            item_id=self.item.pk, claim_token=self.item.claim_token, status="succeeded"
        )
        data = self.client.get(self.feed).json()
        self.assertFalse(data["running"])
        self.assertEqual(data["status"], "Succeeded")

    def test_logs_survive_requeue_and_can_select_previous_run(self):
        self.upload()
        requeue_items(QueueItem.objects.all())
        second_id = uuid.uuid4()
        self.item = claim_next(execution_ref=f"runner:{second_id}")
        response = self.client.get(self.page, {"run": str(self.run_id)})
        self.assertContains(response, "Implementing task")
        self.assertContains(response, 'data-running="false"')

    def test_empty_queue_item_has_helpful_log_page(self):
        response = self.client.get(self.page)
        self.assertContains(response, "Waiting for runner output")
        self.assertContains(response, 'data-cursor="0"')

    def test_logs_are_not_cached(self):
        response = self.client.get(self.feed)
        self.assertIn("no-store", response["Cache-Control"])

    def test_invalid_cursor_is_rejected(self):
        self.assertEqual(
            self.client.get(self.feed, {"after": "invalid"}).status_code, 400
        )

    def test_invalid_run_is_not_found(self):
        self.assertEqual(
            self.client.get(self.page, {"run": "invalid"}).status_code, 404
        )

    def test_another_items_run_cannot_be_selected(self):
        self.assertEqual(
            self.client.get(self.page, {"run": str(uuid.uuid4())}).status_code, 404
        )

    def test_queue_links_to_log_page(self):
        response = self.client.get(reverse("index"))
        self.assertContains(response, f'href="{self.page}"')
