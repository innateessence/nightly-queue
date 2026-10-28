import json
import uuid

from django.db import connection
from django.test import Client, TestCase, TransactionTestCase, override_settings
from django.urls import reverse

from projects.models import Project

from .models import QueueItem
from .services import claim_next, enqueue_item


@override_settings(NQ_API_TOKEN="test-token")
class APITests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.project = Project.objects.create(
            name="Example",
            repository_url="https://github.com/example/repo",
            base_branch="develop",
        )

    def setUp(self):
        self.client = Client(enforce_csrf_checks=True)
        self.client.defaults["HTTP_AUTHORIZATION"] = "Bearer test-token"

    def post(self, name, data, args=None):
        url = reverse(name, args=args)
        body = json.dumps(data)
        return self.client.post(url, body, content_type="application/json")

    def enqueue(self, **fields):
        data = {"project_id": self.project.pk, "title": "Export"}
        data.update(fields)
        return self.post("api_enqueue", data)

    def assert_enqueue_error(self, field, value):
        response = self.enqueue(**{field: value})
        self.assertEqual(response.status_code, 400)
        self.assertIn(field, response.json()["errors"])
        self.assertFalse(QueueItem.objects.exists())

    def finish(self, item, **fields):
        data = {"claim_token": str(item.claim_token), "status": "succeeded"}
        data.update(fields)
        return self.post("api_finish", data, args=[item.pk])

    def test_enqueue_requires_a_token(self):
        response = Client().post(reverse("api_enqueue"))
        self.assertEqual(response.status_code, 401)

    def test_claim_requires_a_token(self):
        response = Client().post(reverse("api_claim"))
        self.assertEqual(response.status_code, 401)

    def test_finish_requires_a_token(self):
        url = reverse("api_finish", args=[1])
        self.assertEqual(Client().post(url).status_code, 401)

    def test_enqueue_persists_an_item_with_defaults(self):
        response = self.enqueue(title=" Export CSV ")
        self.assertEqual(response.status_code, 201)
        item = QueueItem.objects.get()
        self.assertEqual(item.project, self.project)
        self.assertEqual(item.title, "Export CSV")
        self.assertEqual(item.instructions, "")
        self.assertEqual(item.priority, "normal")
        self.assertEqual(item.status, "queued")

    def test_enqueue_response_does_not_expose_a_claim_token(self):
        response = self.enqueue()
        self.assertNotIn("claim_token", response.json()["item"])

    def test_enqueue_rejects_missing_project(self):
        response = self.post("api_enqueue", {"title": "Missing project"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("project_id", response.json()["errors"])
        self.assertFalse(QueueItem.objects.exists())

    def test_enqueue_rejects_null_project(self):
        self.assert_enqueue_error("project_id", None)

    def test_enqueue_rejects_boolean_project(self):
        self.assert_enqueue_error("project_id", True)

    def test_enqueue_rejects_string_project(self):
        self.assert_enqueue_error("project_id", str(self.project.pk))

    def test_enqueue_rejects_unknown_project(self):
        self.assert_enqueue_error("project_id", 99999)

    def test_enqueue_rejects_out_of_range_project(self):
        self.assert_enqueue_error("project_id", 2**64)

    def test_enqueue_rejects_blank_title(self):
        self.assert_enqueue_error("title", " ")

    def test_enqueue_rejects_non_string_title(self):
        self.assert_enqueue_error("title", [])

    def test_enqueue_rejects_long_title(self):
        self.assert_enqueue_error("title", "x" * 201)

    def test_enqueue_rejects_null_instructions(self):
        self.assert_enqueue_error("instructions", None)

    def test_enqueue_rejects_unknown_priority(self):
        self.assert_enqueue_error("priority", "urgent")

    def test_enqueue_rejects_status_override(self):
        response = self.enqueue(status="succeeded")
        self.assertEqual(response.status_code, 400)
        self.assertIn("body", response.json()["errors"])
        self.assertFalse(QueueItem.objects.exists())

    def test_enqueue_rejects_repository_override(self):
        response = self.enqueue(repository_url="https://github.com/other/repo")
        self.assertEqual(response.status_code, 400)
        self.assertFalse(QueueItem.objects.exists())

    def test_malformed_json_is_rejected(self):
        url = reverse("api_enqueue")
        response = self.client.post(url, "{", content_type="application/json")
        self.assertEqual(response.status_code, 400)

    def test_null_json_is_rejected(self):
        response = self.post("api_enqueue", None)
        self.assertEqual(response.status_code, 400)

    def test_json_array_is_rejected(self):
        response = self.post("api_enqueue", [])
        self.assertEqual(response.status_code, 400)

    def test_json_string_is_rejected(self):
        response = self.post("api_enqueue", "text")
        self.assertEqual(response.status_code, 400)

    def test_form_encoded_api_request_is_rejected(self):
        response = self.client.post(reverse("api_enqueue"), {})
        self.assertEqual(response.status_code, 400)

    def test_enqueue_requires_post(self):
        response = self.client.get(reverse("api_enqueue"))
        self.assertEqual(response.status_code, 405)
        self.assertEqual(response["Allow"], "POST")

    def test_claim_returns_execution_settings(self):
        item = enqueue_item(project=self.project, title="Work")
        response = self.post("api_claim", {"execution_ref": "n8n:123"})
        claimed = response.json()["item"]
        self.assertEqual(claimed["id"], item.pk)
        self.assertEqual(claimed["status"], "running")
        self.assertEqual(claimed["repository_url"], self.project.repository_url)
        self.assertEqual(claimed["base_branch"], "develop")
        self.assertEqual(claimed["branch_name"], f"nq/{item.pk}")
        self.assertEqual(claimed["execution_ref"], "n8n:123")
        self.assertIsNotNone(claimed["started_at"])
        self.assertTrue(uuid.UUID(claimed["claim_token"]))

    def test_claim_preserves_oldest_first_order(self):
        first = enqueue_item(project=self.project, title="First", priority="low")
        enqueue_item(project=self.project, title="Second", priority="high")
        response = self.post("api_claim", {})
        self.assertEqual(response.json()["item"]["id"], first.pk)

    def test_claim_skips_running_items(self):
        first = enqueue_item(project=self.project, title="First")
        second = enqueue_item(project=self.project, title="Second")
        claim_next()
        response = self.post("api_claim", {})
        self.assertEqual(response.json()["item"]["id"], second.pk)
        first.refresh_from_db()
        self.assertEqual(first.status, "running")

    def test_claim_returns_null_for_empty_queue(self):
        response = self.post("api_claim", {})
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.json()["item"])

    def test_claim_rejects_long_execution_reference(self):
        response = self.post("api_claim", {"execution_ref": "x" * 201})
        self.assertEqual(response.status_code, 400)
        self.assertIn("execution_ref", response.json()["errors"])

    def test_finish_records_success(self):
        enqueue_item(project=self.project, title="Work")
        item = claim_next()
        url = "https://github.com/example/repo/pull/1"
        response = self.finish(item, result_url=url)
        self.assertEqual(response.status_code, 200)
        item.refresh_from_db()
        self.assertEqual(item.status, "succeeded")
        self.assertEqual(item.result_url, url)
        self.assertIsNotNone(item.finished_at)

    def test_finish_records_failure(self):
        enqueue_item(project=self.project, title="Work")
        item = claim_next()
        response = self.finish(item, status="failed", error_summary="Agent stopped")
        self.assertEqual(response.status_code, 200)
        item.refresh_from_db()
        self.assertEqual(item.status, "failed")
        self.assertEqual(item.error_summary, "Agent stopped")

    def test_finish_rejects_unknown_claim(self):
        enqueue_item(project=self.project, title="Work")
        item = claim_next()
        response = self.finish(item, claim_token=str(uuid.uuid4()))
        self.assertEqual(response.status_code, 409)
        item.refresh_from_db()
        self.assertEqual(item.status, "running")

    def test_finish_rejects_malformed_claim_token(self):
        enqueue_item(project=self.project, title="Work")
        item = claim_next()
        response = self.finish(item, claim_token="bad")
        self.assertEqual(response.status_code, 400)
        self.assertIn("claim_token", response.json()["errors"])

    def test_finish_rejects_non_string_claim_token(self):
        enqueue_item(project=self.project, title="Work")
        item = claim_next()
        response = self.finish(item, claim_token=[])
        self.assertEqual(response.status_code, 400)

    def test_finish_rejects_queued_status(self):
        enqueue_item(project=self.project, title="Work")
        item = claim_next()
        response = self.finish(item, status="queued")
        self.assertEqual(response.status_code, 400)
        self.assertIn("status", response.json()["errors"])

    def test_finish_rejects_javascript_result_url(self):
        enqueue_item(project=self.project, title="Work")
        item = claim_next()
        response = self.finish(item, result_url="javascript:alert(1)")
        self.assertEqual(response.status_code, 400)
        self.assertIn("result_url", response.json()["errors"])

    def test_finish_rejects_ftp_result_url(self):
        enqueue_item(project=self.project, title="Work")
        item = claim_next()
        response = self.finish(item, result_url="ftp://example.com/result")
        self.assertEqual(response.status_code, 400)

    def test_finish_rejects_non_string_error_summary(self):
        enqueue_item(project=self.project, title="Work")
        item = claim_next()
        response = self.finish(item, error_summary={})
        self.assertEqual(response.status_code, 400)

    def test_finish_returns_not_found_for_missing_item(self):
        data = {"claim_token": str(uuid.uuid4()), "status": "succeeded"}
        response = self.post("api_finish", data, args=[99999])
        self.assertEqual(response.status_code, 404)

    def test_finish_rejects_repeated_completion(self):
        enqueue_item(project=self.project, title="Work")
        item = claim_next()
        self.finish(item)
        response = self.finish(item)
        self.assertEqual(response.status_code, 409)


@override_settings(NQ_API_TOKEN="test-token")
class CompletionTransactionTests(TransactionTestCase):
    def test_finish_updates_and_reads_response_in_one_transaction(self):
        url = "https://github.com/example/repo"
        project = Project.objects.create(name="Example", repository_url=url)
        enqueue_item(project=project, title="Work")
        item = claim_next()
        data = {"claim_token": str(item.claim_token), "status": "succeeded"}
        url = reverse("api_finish", args=[item.pk])
        self.client.defaults["HTTP_AUTHORIZATION"] = "Bearer test-token"
        transactions = []

        def record_transaction(execute, sql, params, many, context):
            if sql.startswith(("SELECT", "UPDATE")):
                transactions.append(tuple(connection.atomic_blocks))
            return execute(sql, params, many, context)

        with connection.execute_wrapper(record_transaction):
            body = json.dumps(data)
            response = self.client.post(url, body, content_type="application/json")

        self.assertEqual(response.status_code, 200)
        update_transaction, response_transaction = transactions[-2:]
        self.assertTrue(update_transaction)
        self.assertEqual(update_transaction, response_transaction)
