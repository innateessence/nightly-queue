import json

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from .models import Project, QueueItem


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

    def test_project_listing_requires_a_token(self):
        response = Client().get(reverse("api_projects"))
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response["WWW-Authenticate"], "Bearer")

    def test_enqueue_requires_a_token(self):
        response = Client().post(reverse("api_enqueue"))
        self.assertEqual(response.status_code, 401)

    def test_browser_session_does_not_grant_api_access(self):
        user = get_user_model().objects.create_user(username="browser")
        browser = Client()
        browser.force_login(user)
        response = browser.get(reverse("api_projects"))
        self.assertEqual(response.status_code, 401)

    def test_wrong_token_is_rejected(self):
        self.client.defaults["HTTP_AUTHORIZATION"] = "Bearer wrong-token"
        response = self.client.get(reverse("api_projects"))
        self.assertEqual(response.status_code, 401)

    def test_wrong_authentication_scheme_is_rejected(self):
        self.client.defaults["HTTP_AUTHORIZATION"] = "Basic test-token"
        response = self.client.get(reverse("api_projects"))
        self.assertEqual(response.status_code, 401)

    def test_malformed_authorization_is_rejected(self):
        self.client.defaults["HTTP_AUTHORIZATION"] = "Bearer test-token extra"
        response = self.client.get(reverse("api_projects"))
        self.assertEqual(response.status_code, 401)

    @override_settings(NQ_API_TOKEN="")
    def test_missing_server_token_disables_api(self):
        response = self.client.get(reverse("api_projects"))
        self.assertEqual(response.status_code, 503)

    def test_project_listing_includes_checkout_settings(self):
        expected = {
            "id": self.project.pk,
            "name": "Example",
            "repository_url": "https://github.com/example/repo.git",
            "base_branch": "develop",
        }
        response = self.client.get(reverse("api_projects"))
        self.assertEqual(response.json()["projects"], [expected])

    def test_enqueue_persists_an_item_with_defaults(self):
        response = self.enqueue(title=" Export CSV ")
        self.assertEqual(response.status_code, 201)
        item = QueueItem.objects.get()
        self.assertEqual(item.project, self.project)
        self.assertEqual(item.title, "Export CSV")
        self.assertEqual(item.instructions, "")
        self.assertEqual(item.priority, "normal")

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

    def test_project_listing_requires_get(self):
        response = self.post("api_projects", {})
        self.assertEqual(response.status_code, 405)
        self.assertEqual(response["Allow"], "GET")
