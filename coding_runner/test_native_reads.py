from email.message import Message
from types import SimpleNamespace
import unittest
import subprocess
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

from vibearr_runner.delivery import (
    DeliveryError,
    DeliveryReadUnavailable,
    DeliveryUnknown,
    SupervisorGit,
)
from vibearr_runner.native_github import NativeGitHub
from vibearr_runner.native_reads import NativeReadExhausted, NativeReadWait, read


class NativeReadTests(unittest.TestCase):
    def setUp(self):
        self.host = SimpleNamespace(native={}, save=Mock())

    def test_backoff_and_budget_survive_a_new_supervisor(self):
        operation = Mock(side_effect=DeliveryReadUnavailable("DNS"))
        with patch("vibearr_runner.native_reads.time.time", return_value=100):
            with self.assertRaises(NativeReadWait):
                read(self.host, "GET repository", operation)
            restarted = SimpleNamespace(native=self.host.native, save=Mock())
            with self.assertRaises(NativeReadWait):
                read(restarted, "GET repository", operation)
        self.assertEqual(operation.call_count, 1)
        for now, error in [
            (101, NativeReadWait),
            (103, NativeReadExhausted),
            (999, NativeReadExhausted),
        ]:
            with patch("vibearr_runner.native_reads.time.time", return_value=now):
                with self.assertRaises(error):
                    read(restarted, "GET repository", operation)
        self.assertEqual(operation.call_count, 3)

    def test_success_after_transient_failure_clears_only_that_read(self):
        operation = Mock(side_effect=[DeliveryReadUnavailable("DNS"), {"id": 17}])
        with patch("vibearr_runner.native_reads.time.time", return_value=100):
            with self.assertRaises(NativeReadWait):
                read(self.host, "repository", operation)
        with patch("vibearr_runner.native_reads.time.time", return_value=101):
            self.assertEqual(read(self.host, "repository", operation), {"id": 17})
        self.assertEqual(self.host.native["transport_reads"], {})

    def test_rate_limit_headers_delay_the_next_attempt(self):
        headers = Message()
        headers["Retry-After"] = "120"
        headers["X-RateLimit-Remaining"] = "0"
        headers["X-RateLimit-Reset"] = "500"
        github = NativeGitHub(
            "secret", read=lambda key, operation: read(self.host, key, operation)
        )
        error = HTTPError(
            "https://api.github.com/repositories/17", 403, "limited", headers, None
        )
        with patch.object(github.opener, "open", side_effect=error) as opened:
            with patch("time.time", return_value=100):
                with self.assertRaises(NativeReadWait):
                    github.repository(repository_id=17)
            with patch("time.time", return_value=499):
                with self.assertRaises(NativeReadWait):
                    github.repository(repository_id=17)
        self.assertEqual(opened.call_count, 1)
        self.assertEqual(
            next(iter(self.host.native["transport_reads"].values()))["retry_at"], 500
        )

    def test_permanent_read_rejection_is_not_retryable(self):
        github = NativeGitHub("secret")
        error = HTTPError(
            "https://api.github.com/repositories/17", 401, "denied", Message(), None
        )
        with patch.object(github.opener, "open", side_effect=error):
            with self.assertRaises(DeliveryError) as caught:
                github.repository(repository_id=17)
        self.assertNotIsInstance(caught.exception, DeliveryReadUnavailable)

    def test_server_read_failure_is_retryable(self):
        github = NativeGitHub("secret")
        error = HTTPError(
            "https://api.github.com/repositories/17",
            503,
            "unavailable",
            Message(),
            None,
        )
        with patch.object(github.opener, "open", side_effect=error):
            with self.assertRaises(DeliveryReadUnavailable):
                github.repository(repository_id=17)

    def test_git_remote_dns_failure_uses_the_same_bounded_read_budget(self):
        transport = SupervisorGit(
            token="secret", read=lambda key, operation: read(self.host, key, operation)
        )
        result = subprocess.CompletedProcess(
            [], 128, b"", b"fatal: Could not resolve host: github.com"
        )
        with patch(
            "vibearr_runner.delivery.processes.run", return_value=result
        ) as executed:
            with self.assertRaises(NativeReadWait):
                transport.command(
                    ["ls-remote", "--", "https://github.com/owner/repo.git"],
                    "https://github.com/owner/repo.git",
                    "/tmp",
                )
            with self.assertRaises(NativeReadWait):
                transport.command(
                    ["ls-remote", "--", "https://github.com/owner/repo.git"],
                    "https://github.com/owner/repo.git",
                    "/tmp",
                )
        self.assertEqual(executed.call_count, 1)

    def test_mutation_network_failure_remains_unknown_without_read_retry(self):
        callback = Mock()
        github = NativeGitHub("secret", read=callback)
        with patch.object(github.opener, "open", side_effect=URLError("DNS")):
            with self.assertRaises(DeliveryUnknown):
                github.request("POST", "/repos/owner/repo/pulls", {})
        callback.assert_not_called()
