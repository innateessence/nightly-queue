import copy
import unittest
from unittest.mock import Mock

from vibearr_runner.delivery import DeliveryError, GitHubDeliveryClient, pull_identity


class PullIdentityTests(unittest.TestCase):
    def setUp(self):
        self.raw = {
            "number": 1,
            "html_url": "https://github.com/example/repo/pull/1",
            "state": "open",
            "head": {
                "repo": {"full_name": "example/repo"},
                "ref": "vibearr/task",
                "sha": "a" * 40,
            },
            "base": {"repo": {"full_name": "example/repo"}, "ref": "main"},
        }

    def test_actual_repository_head_base_and_sha_are_required(self):
        self.assertEqual(
            pull_identity(self.raw, "example/repo", "vibearr/task", "main", "a" * 40)[
                "number"
            ],
            1,
        )

    def test_wrong_repo_or_head_or_empty_url_are_rejected(self):
        for key, value in (
            ("html_url", ""),
            ("html_url", "https://github.com/other/repo/pull/1"),
            ("state", "closed"),
        ):
            raw = {**self.raw, key: value}
            with self.subTest(key=key, value=value), self.assertRaises(DeliveryError):
                pull_identity(raw, "example/repo", "vibearr/task", "main", "a" * 40)
        raw = copy.deepcopy(self.raw)
        raw["head"]["ref"] = "another"
        with self.assertRaises(DeliveryError):
            pull_identity(raw, "example/repo", "vibearr/task", "main", "a" * 40)

    def test_github_client_refuses_wrong_returned_repository(self):
        client = GitHubDeliveryClient("test-token")
        client.request = Mock(
            return_value=[
                {**self.raw, "html_url": "https://github.com/evil/repo/pull/1"}
            ]
        )
        with self.assertRaises(DeliveryError):
            client.find("example/repo", "vibearr/task", "main", "a" * 40)

    def test_github_client_disables_redirects_and_environment_proxies(self):
        client = GitHubDeliveryClient("test-token")
        from vibearr_runner.client import NoRedirects

        self.assertTrue(
            any(isinstance(handler, NoRedirects) for handler in client.opener.handlers)
        )
