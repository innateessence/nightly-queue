import json
from datetime import timedelta
from io import BytesIO
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit

from django.conf import settings
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from credentials.models import APISettings, Credential
from credentials.test_models import TEST_KEY
from vibearr_app.celery import app
from projects.models import Project

from .github import GitHubAPIError, NoRedirect, repository_issues
from .models import QueueItem
from .test_selection import source_issue
from .services import claim_next, finish_item, import_issue
from .tasks import import_auto_issues


def issue(number=1, **changes):
    data = {
        "number": number,
        "title": f"Issue {number}",
        "body": "Issue context",
        "state": "open",
        "id": 1000 + number,
        "user": {"id": 42, "login": "owner"},
        "labels": [],
        **{
            key: value
            for key, value in source_issue(number).items()
            if key not in ("number", "title", "body", "state")
        },
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
            [
                {key: result[key] for key in ("number", "title", "body", "state")}
                for result in results
            ],
            [{"number": 2, "title": "Issue 2", "body": "", "state": "closed"}],
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

    def test_real_celery_configuration_checks_scheduled_import_every_minute(self):
        schedule = app.conf.beat_schedule["scheduled-auto-issues"]
        self.assertEqual(schedule["task"], import_auto_issues.name)
        self.assertEqual(schedule["schedule"], 60.0)
        self.assertEqual(schedule["kwargs"], {"scheduled": True})
        self.assertEqual(app.conf.broker_url, settings.CELERY_BROKER_URL)


@override_settings(NQ_CREDENTIAL_KEY=TEST_KEY)
class AutoImportTests(TestCase):
    def setUp(self):
        metadata = patch(
            "work_queue.tasks.repository_metadata",
            side_effect=lambda url, token, identity=None: dict(
                repository_id=99, owner_id=42, repository_url=url, default_branch="main"
            ),
        )
        metadata.start()
        self.addCleanup(metadata.stop)
        self.project = Project.objects.create(
            name="Auto",
            repository_url="https://github.com/example/auto",
            autonomous=True,
            github_repository_id=99,
            github_owner_id=42,
            base_branch="main",
        )
        self.manual = Project.objects.create(
            name="Manual", repository_url="https://github.com/example/manual"
        )
        credential = Credential(name="API token", provider="github", kind="token")
        credential.set_secret("selected-private-token")
        credential.save()
        APISettings.objects.create(github_credential=credential)

    @patch("work_queue.tasks.repository_issues", return_value=[])
    def test_first_scheduled_sync_runs_immediately(self, fetch):
        result = import_auto_issues(scheduled=True)
        self.assertEqual(result["projects"], 1)
        self.assertIsNotNone(APISettings.objects.get().last_auto_sync_at)
        fetch.assert_called_once()

    @patch("work_queue.tasks.repository_issues", return_value=[])
    def test_scheduled_sync_waits_for_each_supported_interval(self, fetch):
        now = timezone.now()
        for hours in [1, 4, 8, 12, 24]:
            with self.subTest(hours=hours):
                last_sync = now - timedelta(hours=hours) + timedelta(seconds=1)
                APISettings.objects.update(
                    auto_sync_interval_seconds=hours * 3600, last_auto_sync_at=last_sync
                )
                with patch("work_queue.services.timezone.now", return_value=now):
                    self.assertTrue(import_auto_issues(scheduled=True)["skipped"])
                fetch.assert_not_called()

    @patch("work_queue.tasks.repository_issues", return_value=[])
    def test_scheduled_sync_runs_at_each_supported_interval(self, fetch):
        now = timezone.now()
        for hours in [1, 4, 8, 12, 24]:
            with self.subTest(hours=hours):
                last_sync = now - timedelta(hours=hours)
                APISettings.objects.update(
                    auto_sync_interval_seconds=hours * 3600, last_auto_sync_at=last_sync
                )
                with patch("work_queue.services.timezone.now", return_value=now):
                    self.assertEqual(import_auto_issues(scheduled=True)["projects"], 1)
                self.assertEqual(APISettings.objects.get().last_auto_sync_at, now)
                fetch.assert_called_once()
                fetch.reset_mock()

    @patch("work_queue.tasks.repository_issues", return_value=[])
    def test_duplicate_scheduled_sync_is_skipped(self, fetch):
        import_auto_issues(scheduled=True)
        self.assertTrue(import_auto_issues(scheduled=True)["skipped"])
        fetch.assert_called_once()

    @patch("work_queue.tasks.repository_issues", return_value=[])
    def test_shortening_interval_applies_to_next_check(self, fetch):
        last_sync = timezone.now() - timedelta(hours=5)
        APISettings.objects.update(
            auto_sync_interval_seconds=86400, last_auto_sync_at=last_sync
        )
        self.assertTrue(import_auto_issues(scheduled=True)["skipped"])
        APISettings.objects.update(auto_sync_interval_seconds=14400)
        self.assertEqual(import_auto_issues(scheduled=True)["projects"], 1)
        fetch.assert_called_once()

    @patch("work_queue.tasks.repository_issues", return_value=[])
    def test_manual_sync_bypasses_interval_without_postponing_scheduled_sync(
        self, fetch
    ):
        last_sync = timezone.now()
        APISettings.objects.update(
            auto_sync_interval_seconds=86400, last_auto_sync_at=last_sync
        )
        self.assertEqual(import_auto_issues()["projects"], 1)
        self.assertEqual(APISettings.objects.get().last_auto_sync_at, last_sync)
        fetch.assert_called_once()

    @override_settings(NQ_CREDENTIAL_KEY="")
    @patch("work_queue.tasks.repository_issues")
    def test_skipped_scheduled_sync_does_not_advance_timer(self, fetch):
        with self.assertLogs("work_queue.tasks", level="ERROR"):
            self.assertTrue(import_auto_issues(scheduled=True)["skipped"])
        self.assertIsNone(APISettings.objects.get().last_auto_sync_at)
        fetch.assert_not_called()

    @patch("work_queue.tasks.repository_issues", side_effect=GitHubAPIError("failed"))
    def test_failed_import_waits_until_next_scheduled_sync(self, fetch):
        with self.assertLogs("work_queue.tasks", level="WARNING"):
            self.assertEqual(import_auto_issues(scheduled=True)["failed"], 1)
        self.assertTrue(import_auto_issues(scheduled=True)["skipped"])
        fetch.assert_called_once()

    @patch("work_queue.tasks.repository_issues")
    def test_import_uses_selected_token_and_only_autonomous_projects(self, fetch):
        fetch.return_value = [issue()]
        result = import_auto_issues()
        fetch.assert_called_once_with(
            self.project.repository_url, "selected-private-token", etags={}
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
            github_repository_id=99,
            github_owner_id=42,
            base_branch="main",
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
        Project.objects.filter(pk=self.project.pk).update(
            repository_url="https://github.com/example/renamed.git"
        )
        self.project.refresh_from_db()
        import_issue(project=self.project, **issue())
        self.assertEqual(QueueItem.objects.count(), 1)

    def test_same_number_in_different_projects_is_imported(self):
        import_issue(project=self.project, **issue())
        import_issue(project=self.manual, **issue())
        self.assertEqual(QueueItem.objects.count(), 2)

    def test_long_issue_titles_do_not_block_import(self):
        import_issue(project=self.project, **issue(title="x" * 256))
        self.assertEqual(len(QueueItem.objects.get().title), 200)

    @patch("work_queue.tasks.repository_issues", return_value=[])
    def test_discovery_continues_while_native_execution_is_paused(self, fetch):
        from runner_control.settings_models import RunnerSettings

        RunnerSettings.objects.filter(pk=1).update(enabled=False, paused=True)
        self.assertEqual(import_auto_issues()["projects"], 1)
        fetch.assert_called_once()

    @patch("work_queue.tasks.repository_issues", return_value=[])
    def test_per_project_lease_prevents_overlapping_manual_sync(self, fetch):
        nested = []

        def repository(url, token, identity):
            nested.append(import_auto_issues())
            return dict(
                repository_id=99, owner_id=42, repository_url=url, default_branch="main"
            )

        with patch("work_queue.tasks.repository_metadata", side_effect=repository):
            self.assertEqual(import_auto_issues()["projects"], 1)
        self.assertEqual(nested[0]["projects"], 0)
        fetch.assert_called_once()

    @patch("work_queue.tasks.repository_issues", side_effect=GitHubAPIError("offline"))
    def test_failed_attempt_does_not_advance_success_timestamp(self, fetch):
        last_success = timezone.now() - timedelta(days=1)
        APISettings.objects.update(last_auto_sync_success_at=last_success)
        with self.assertLogs("work_queue.tasks", level="WARNING"):
            import_auto_issues()
        self.assertEqual(
            APISettings.objects.get().last_auto_sync_success_at, last_success
        )
        self.project.refresh_from_db()
        self.assertIsNone(self.project.sync_success_at)
        self.assertIsNotNone(self.project.sync_attempt_at)
        self.assertIsNotNone(self.project.sync_not_before)

    @patch("work_queue.tasks.repository_issues")
    def test_rename_binds_legacy_identity_without_duplicating_work(self, fetch):
        old = QueueItem.objects.create(
            project=self.project,
            source="github",
            issue_number=1,
            issue_repository=self.project.repository_url,
            title="Legacy",
        )
        fetch.return_value = [issue()]
        renamed = "https://github.com/example/renamed.git"
        repository = dict(
            repository_id=99, owner_id=42, repository_url=renamed, default_branch="main"
        )
        with patch("work_queue.tasks.repository_metadata", return_value=repository):
            import_auto_issues()
        self.assertEqual(QueueItem.objects.count(), 1)
        old.refresh_from_db()
        self.assertEqual(old.github_repository_id, 99)
        self.assertEqual(old.issue_repository, renamed)

    @patch("work_queue.tasks.repository_issues")
    def test_blank_base_follows_changed_remote_default_for_new_claims(self, fetch):
        Project.objects.filter(pk=self.project.pk).update(base_branch="")
        repository = dict(
            repository_id=99,
            owner_id=42,
            repository_url=self.project.repository_url,
            default_branch="main",
        )
        fetch.return_value = [issue(1)]
        with patch("work_queue.tasks.repository_metadata", return_value=repository):
            import_auto_issues()
        first = claim_next()
        self.assertEqual(first.base_branch, "main")
        self.project.refresh_from_db()
        self.assertEqual(self.project.base_branch, "")
        self.assertEqual(self.project.github_default_branch, "main")
        fetch.return_value = [issue(1), issue(2)]
        with patch(
            "work_queue.tasks.repository_metadata",
            return_value={**repository, "default_branch": "trunk"},
        ):
            import_auto_issues()
        self.assertEqual(claim_next().base_branch, "trunk")
        first.refresh_from_db()
        self.assertEqual(first.base_branch, "main")
        self.project.refresh_from_db()
        self.assertEqual(self.project.base_branch, "")

    @patch("work_queue.tasks.repository_issues")
    def test_explicit_base_overrides_remote_default(self, fetch):
        Project.objects.filter(pk=self.project.pk).update(base_branch="release")
        fetch.return_value = [issue()]
        repository = dict(
            repository_id=99,
            owner_id=42,
            repository_url=self.project.repository_url,
            default_branch="trunk",
        )
        with patch("work_queue.tasks.repository_metadata", return_value=repository):
            import_auto_issues()
        self.assertEqual(claim_next().base_branch, "release")

    @patch("work_queue.tasks.repository_issues")
    def test_oversized_foreign_dependency_does_not_abort_later_projects(self, fetch):
        other = Project.objects.create(
            name="Second",
            repository_url="https://github.com/example/second",
            autonomous=True,
        )
        hostile = issue(body="Depends on: #" + "9" * 5000, author_id=1337)
        fetch.side_effect = [[hostile], [issue(2)]]
        result = import_auto_issues()
        self.assertEqual(result["projects"], 2)
        self.assertEqual(result["failed"], 0)
        self.assertIn(
            "too large", QueueItem.objects.get(project=self.project).selection_error
        )
        self.assertTrue(
            QueueItem.objects.filter(project=other, issue_number=2).exists()
        )

    @patch("work_queue.tasks.repository_issues")
    def test_issue_page_cannot_apply_after_newer_repository_observation(self, fetch):
        from .services import accept_repository_observation, repository_authority_state
        from .test_selection import source_repository

        import_issue(project=self.project, **issue())
        item = QueueItem.objects.get(project=self.project)
        expected_item = QueueItem.objects.values().get(pk=item.pk)

        def page(*args, **kwargs):
            current = Project.objects.get(pk=self.project.pk)
            accept_repository_observation(
                project=current,
                repository=source_repository(
                    owner_id=777, repository_url=current.repository_url
                ),
                expected=repository_authority_state(current),
            )
            return [issue(state="closed", labels=["vibearr:hold"])]

        fetch.side_effect = page
        with self.assertLogs("work_queue.tasks", level="WARNING"):
            result = import_auto_issues()
        self.assertEqual(result["failed"], 1)
        self.assertEqual(result["projects"], 0)
        self.assertEqual(QueueItem.objects.values().get(pk=item.pk), expected_item)
        self.project.refresh_from_db()
        self.assertEqual(self.project.github_owner_id, 777)
        self.assertEqual(self.project.github_authority_version, 2)
        self.assertIsNone(self.project.sync_success_at)


class ConditionalImportTests(TestCase):
    @patch("work_queue.github.github_read")
    def test_not_modified_page_reuses_validated_cached_source(self, read):
        cached = {"1": {"etag": '"version-1"', "issues": [issue()]}}
        read.return_value = (None, {"not_modified": "true"})
        results = list(
            repository_issues("https://github.com/example/repo", "token", etags=cached)
        )
        self.assertEqual(results[0]["author_id"], 42)
        self.assertEqual(read.call_args.args[2], '"version-1"')

    @patch("work_queue.github.github_read")
    def test_missing_conditional_cache_is_an_error(self, read):
        read.return_value = (None, {})
        with self.assertRaises(GitHubAPIError):
            list(
                repository_issues("https://github.com/example/repo", "token", etags={})
            )

    def test_rate_limit_headers_bound_retry_time(self):
        from .github import retry_time

        now = timezone.now()
        retry = retry_time({"Retry-After": "300"})
        self.assertGreaterEqual(retry, now + timedelta(seconds=300))

    def test_extreme_rate_limit_headers_do_not_overflow(self):
        from .github import retry_time

        now = timezone.now()
        deadline = retry_time(
            {"Retry-After": "9" * 19, "X-RateLimit-Reset": "-" + "9" * 19}
        )
        self.assertGreaterEqual(deadline, now + timedelta(seconds=60))
        self.assertLessEqual(deadline, timezone.now() + timedelta(days=1))
