import json

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from .models import Project


@override_settings(NQ_API_TOKEN="test-token")
class ProjectAPITests(TestCase):
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

    def post(self, name, data):
        url = reverse(name)
        body = json.dumps(data)
        return self.client.post(url, body, content_type="application/json")

    def test_project_listing_requires_a_token(self):
        response = Client().get(reverse("api_projects"))
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response["WWW-Authenticate"], "Bearer")

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

    def test_project_listing_requires_get(self):
        response = self.post("api_projects", {})
        self.assertEqual(response.status_code, 405)
        self.assertEqual(response["Allow"], "GET")
