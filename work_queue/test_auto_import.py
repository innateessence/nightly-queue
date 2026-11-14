import json
from io import BytesIO
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit

from django.conf import settings
from django.test import SimpleTestCase, TestCase, override_settings

from credentials.models import APISettings, Credential
from credentials.test_models import TEST_KEY
from nightly_queue.celery import app
from projects.models import Project

from .github import GitHubAPIError, NoRedirect, repository_issues
from .models import QueueItem
from .services import claim_next, finish_item, import_issue
from .tasks import import_auto_issues


def issue(number=1, **changes):
    data = {
        "number": number,
        "title": f"Issue {number}",
        "body": "Issue context",
        "state": "open",
    }
    data.update(changes)
    return data


class GitHubClientTests(SimpleTestCase):
    def response(self, data):
        return BytesIO(json.dumps(data).encode())

    @patch("work_queue.github.build_opener")
    def test_fetches_every_page_with_authentication(self, build_opener):
        opener = build_opener.return_value
        first = [issue(number) for number in range(1, 101)]
        opener.open.side_effect = [self.response(first), self.response([issue(101)])]
        results = list(
            repository_issues("https://github.com/example/repo.git", "api-token")
        )
        self.assertEqual(len(results), 101)
        request = opener.open.call_args_list[0].args[0]
        self.assertEqual(request.get_header("Authorization"), "Bearer api-token")
        self.assertEqual(urlsplit(request.full_url).netloc, "api.github.com")
        self.assertEqual(urlsplit(request.full_url).path, "/repos/example/repo/issues")
        query = parse_qs(urlsplit(request.full_url).query)
        self.assertEqual(query["state"], ["all"])
        self.assertEqual(query["per_page"], ["100"])
        second = opener.open.call_args_list[1].args[0]
        self.assertEqual(parse_qs(urlsplit(second.full_url).query)["page"], ["2"])
        self.assertEqual(opener.open.call_args.kwargs["timeout"], 30)

    @patch("work_queue.github.build_opener")
    def test_skips_pull_requests_and_accepts_null_body(self, build_opener):
        data = [issue(1, pull_request={}), issue(2, body=None, state="closed")]
        build_opener.return_value.open.return_value = self.response(data)
        results = list(repository_issues("https://github.com/example/repo", "token"))
        self.assertEqual(
            results, [{"number": 2, "title": "Issue 2", "body": "", "state": "closed"}]
        )

    @patch("work_queue.github.build_opener")
    def test_http_errors_do_not_expose_response_or_token(self, build_opener):
        build_opener.return_value.open.side_effect = HTTPError(
            "https://api.github.com", 403, "private-token", {}, None
        )
        with self.assertRaisesMessage(GitHubAPIError, "GitHub returned HTTP 403."):
            list(repository_issues("https://github.com/example/repo", "private-token"))

    @patch("work_queue.github.build_opener")
    def test_network_error_is_reported(self, build_opener):
        build_opener.return_value.open.side_effect = URLError("connection failed")
        with self.assertRaises(GitHubAPIError):
            list(repository_issues("https://github.com/example/repo", "token"))

    @patch("work_queue.github.build_opener")
    def test_invalid_response_is_rejected(self, build_opener):
        build_opener.return_value.open.return_value = self.response(
            {"message": "error"}
        )
        with self.assertRaises(GitHubAPIError):
            list(repository_issues("https://github.com/example/repo", "token"))

    def test_redirects_are_not_followed_with_credentials(self):
        self.assertIsNone(
            NoRedirect().redirect_request(
                None, None, 302, "", {}, "https://example.com"
            )
        )

    def test_real_celery_configuration_schedules_import_hourly(self):
        schedule = app.conf.beat_schedule["hourly-auto-issues"]
        self.assertEqual(schedule["task"], import_auto_issues.name)
        self.assertEqual(schedule["schedule"], 3600.0)
        self.assertEqual(app.conf.broker_url, settings.CELERY_BROKER_URL)


@override_settings(NQ_CREDENTIAL_KEY=TEST_KEY)
class AutoImportTests(TestCase):
    def setUp(self):
        self.project = Project.objects.create(
            name="Auto",
            repository_url="https://github.com/example/auto",
            autonomous=True,
        )
        self.manual = Project.objects.create(
            name="Manual", repository_url="https://github.com/example/manual"
        )
        credential = Credential(name="API token", provider="github", kind="token")
        credential.set_secret("selected-private-token")
        credential.save()
        APISettings.objects.create(github_credential=credential)

    @patch("work_queue.tasks.repository_issues")
    def test_import_uses_selected_token_and_only_autonomous_projects(self, fetch):
        fetch.return_value = [issue()]
        result = import_auto_issues()
        fetch.assert_called_once_with(
            self.project.repository_url, "selected-private-token"
        )
        item = QueueItem.objects.get()
        self.assertEqual(item.source, QueueItem.Source.GITHUB)
        self.assertEqual(item.instructions, "Issue context")
        self.assertEqual(item.issue_number, 1)
        self.assertEqual(item.issue_repository, self.project.repository_url)
        self.assertEqual(result["created"], 1)

    @patch("work_queue.tasks.repository_issues")
    def test_repeated_import_refreshes_without_duplicates(self, fetch):
        fetch.return_value = [issue()]
        import_auto_issues()
        fetch.return_value = [
            issue(title="Updated title", body="Updated context", state="closed")
        ]
        result = import_auto_issues()
        item = QueueItem.objects.get()
        self.assertEqual(item.title, "Updated title")
        self.assertEqual(item.instructions, "Updated context")
        self.assertEqual(item.issue_state, "closed")
        self.assertEqual(result["created"], 0)

    @patch("work_queue.tasks.repository_issues")
    def test_project_failure_does_not_stop_remaining_projects(self, fetch):
        other = Project.objects.create(
            name="Second",
            repository_url="https://github.com/example/second",
            autonomous=True,
        )
        fetch.side_effect = [GitHubAPIError("failed"), [issue()]]
        with self.assertLogs("work_queue.tasks", level="WARNING"):
            result = import_auto_issues()
        self.assertEqual(QueueItem.objects.get().project, other)
        self.assertEqual(result["failed"], 1)
        self.assertEqual(result["projects"], 1)

    @patch("work_queue.tasks.repository_issues")
    def test_missing_selection_skips_network_access(self, fetch):
        APISettings.objects.all().delete()
        self.assertTrue(import_auto_issues()["skipped"])
        fetch.assert_not_called()

    @override_settings(NQ_CREDENTIAL_KEY="")
    @patch("work_queue.tasks.repository_issues")
    def test_missing_encryption_key_does_not_expose_secret(self, fetch):
        with self.assertLogs("work_queue.tasks", level="ERROR") as logs:
            self.assertTrue(import_auto_issues()["skipped"])
        fetch.assert_not_called()
        self.assertNotIn("selected-private-token", str(logs.output))

    def test_import_preserves_completed_work_and_claim_checkout(self):
        import_issue(project=self.project, **issue())
        claimed = claim_next()
        finish_item(
            item_id=claimed.pk, claim_token=claimed.claim_token, status="succeeded"
        )
        import_issue(project=self.project, **issue(title="New title", body="New text"))
        claimed.refresh_from_db()
        self.assertEqual(claimed.status, "succeeded")
        self.assertEqual(claimed.instructions, "Issue context")
        self.assertEqual(claimed.repository_url, self.project.repository_url)
        self.assertIsNone(claim_next())

    def test_repository_change_keeps_issue_identity_separate(self):
        import_issue(project=self.project, **issue())
        self.project.repository_url = "https://github.com/example/replacement"
        self.project.save()
        import_issue(project=self.project, **issue())
        self.assertEqual(QueueItem.objects.count(), 2)

    def test_same_number_in_different_projects_is_imported(self):
        import_issue(project=self.project, **issue())
        import_issue(project=self.manual, **issue())
        self.assertEqual(QueueItem.objects.count(), 2)

    def test_long_issue_titles_do_not_block_import(self):
        import_issue(project=self.project, **issue(title="x" * 256))
        self.assertEqual(len(QueueItem.objects.get().title), 200)
