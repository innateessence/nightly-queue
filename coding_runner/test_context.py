import copy
import io
import json
import threading
import time
import unittest
from unittest.mock import patch

from .context import (
    ContextError,
    GitHubContextClient,
    discussion_digest,
    manual_context,
)
from .plans import validate_context


ITEM = {
    "source": "github",
    "issue_repository": "https://github.com/original/issues.git",
    "issue_number": 7,
    "repository_url": "https://github.com/checkout/code.git",
    "base_branch": "main",
    "branch_name": "task-7",
    "title": "Imported old title",
    "instructions": "Old body",
    "project": {"repository_url": "https://github.com/changed/project.git"},
}
URL = "https://github.com/original/issues/issues/7"
NOW = "2026-11-15T12:00:00Z"


def issue(**changes):
    result = {
        "number": 7,
        "title": "Current title",
        "body": "Current body",
        "state": "open",
        "comments": 0,
        "user": {"login": "author"},
        "created_at": NOW,
        "updated_at": NOW,
        "html_url": URL,
    }
    return {**result, **changes}


def comment(identifier=1):
    return {
        "id": identifier,
        "body": "Please consider a regression",
        "user": {"login": "reviewer"},
        "created_at": NOW,
        "updated_at": NOW,
        "html_url": URL + f"#issuecomment-{identifier}",
    }


class Response(io.BytesIO):
    status = 200

    def __init__(self, data):
        super().__init__(json.dumps(data).encode())
        self.headers = {}


class GitHubContextTests(unittest.TestCase):
    def setUp(self):
        self.client = GitHubContextClient("selected-issue-read-token")

    def responses(self, *responses):
        return patch.object(self.client, "_get", side_effect=responses)

    def test_local_stop_bounds_stalled_issue_transport_and_preserves_lock(self):
        stopped = threading.Event()
        release = threading.Event()
        timer = threading.Timer(0.05, stopped.set)
        callback_threads = []

        def checkpoint():
            callback_threads.append(threading.get_ident())
            return not stopped.is_set()

        def stalled(*args, **kwargs):
            release.wait(2)
            raise TimeoutError

        try:
            with patch.object(
                self.client.opener, "open", side_effect=stalled
            ) as transport:
                timer.start()
                started = time.monotonic()
                with self.assertRaisesRegex(ContextError, "interrupted"):
                    self.client._get(
                        "https://api.github.com/repos/original/issues/issues/7",
                        checkpoint=checkpoint,
                    )
                self.assertLess(time.monotonic() - started, 0.7)
                with self.assertRaisesRegex(ContextError, "previous"):
                    self.client._get(
                        "https://api.github.com/repos/original/issues/issues/7"
                    )
                self.assertEqual(transport.call_count, 1)
        finally:
            timer.cancel()
            release.set()
        self.assertTrue(self.client.request_lock.acquire(timeout=2))
        self.client.request_lock.release()
        self.assertEqual(set(callback_threads), {threading.get_ident()})

    def test_complete_empty_comments_is_explicit(self):
        with self.responses((issue(), ""), ([], "")):
            result = self.client.fetch(ITEM)
        self.assertEqual(result["comments"], [])
        self.assertEqual(result["title"], "Current title")
        self.assertEqual(result["issue"]["author"], "author")
        validate_context(result, ITEM)

    def test_aggregate_deadline_stops_comment_pagination(self):
        clock = [0.0]
        link = '<https://api.github.com/repos/original/issues/issues/7/comments?per_page=100&page=2>; rel="next"'
        pages = [(issue(comments=2), ""), ([comment()], link), ([comment(2)], "")]

        def page(*args, **kwargs):
            clock[0] += 0.6
            return pages.pop(0)

        with patch(
            "coding_runner.context.time.monotonic", side_effect=lambda: clock[0]
        ):
            with patch.object(self.client, "_get", side_effect=page) as fetch:
                with self.assertRaisesRegex(ContextError, "budget"):
                    self.client.fetch(ITEM, deadline=1.0)
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(len(pages), 1)

    def test_gate_closure_stops_before_next_comment_page(self):
        allowed = [True]
        link = '<https://api.github.com/repos/original/issues/issues/7/comments?per_page=100&page=2>; rel="next"'
        pages = [(issue(comments=2), ""), ([comment()], link), ([comment(2)], "")]

        def page(*args, **kwargs):
            response = pages.pop(0)
            if len(pages) == 1:
                allowed[0] = False
            return response

        with patch.object(self.client, "_get", side_effect=page) as fetch:
            with self.assertRaisesRegex(ContextError, "interrupted"):
                self.client.fetch(ITEM, checkpoint=lambda: allowed[0])
        self.assertEqual(fetch.call_count, 2)

    def test_request_timeout_is_capped_by_aggregate_remaining_time(self):
        opener = unittest.mock.Mock()
        opener.open.return_value = Response(issue())
        self.client.opener = opener
        with patch("coding_runner.context.time.monotonic", return_value=10.0):
            self.client._get(
                "https://api.github.com/repos/original/issues/issues/7", deadline=10.2
            )
        self.assertAlmostEqual(opener.open.call_args.kwargs["timeout"], 0.2)

    def test_fetches_every_comment_page_from_frozen_issue_repository(self):
        next_page = "https://api.github.com/repos/original/issues/issues/7/comments?page=2&per_page=100"
        link = f'<{next_page}>; rel="next"'
        with self.responses(
            (issue(comments=2), ""), ([comment()], link), ([comment(2)], "")
        ) as fetch:
            result = self.client.fetch(ITEM)
        self.assertEqual(len(result["comments"]), 2)
        self.assertEqual(
            fetch.call_args_list[0].args[0],
            "https://api.github.com/repos/original/issues/issues/7",
        )
        self.assertEqual(fetch.call_args_list[-1].args[0], next_page)
        self.assertEqual(result["checkout"]["repository_url"], ITEM["repository_url"])
        self.assertEqual(result["comments"][1]["updated_at"], NOW)

    def test_unavailable_later_page_is_not_an_empty_discussion(self):
        link = '<https://api.github.com/repos/original/issues/issues/7/comments?per_page=100&page=2>; rel="next"'
        with self.responses(
            (issue(comments=2), ""), ([comment()], link), ContextError("Unavailable")
        ):
            with self.assertRaises(ContextError):
                self.client.fetch(ITEM)

    def test_malformed_page_is_not_an_empty_discussion(self):
        with self.responses((issue(), ""), ({"error": "no comments"}, "")):
            with self.assertRaises(ContextError):
                self.client.fetch(ITEM)

    def test_closed_issue_stops_without_comment_request(self):
        with self.responses((issue(state="closed"), "")) as fetch:
            with self.assertRaisesRegex(ContextError, "closed"):
                self.client.fetch(ITEM)
        self.assertEqual(fetch.call_count, 1)

    def test_null_body_is_a_valid_empty_description(self):
        with self.responses((issue(body=None), ""), ([], "")):
            result = self.client.fetch(ITEM)
        self.assertEqual(result["instructions"], "")

    def test_wrong_body_type_is_rejected(self):
        with self.responses((issue(body=[]), "")):
            with self.assertRaises(ContextError):
                self.client.fetch(ITEM)

    def test_foreign_next_page_is_rejected_before_credentials_are_sent(self):
        with self.responses(
            (issue(), ""), ([], '<https://evil.test/comments>; rel="next"')
        ) as fetch:
            with self.assertRaisesRegex(ContextError, "pagination"):
                self.client.fetch(ITEM)
        self.assertEqual(fetch.call_count, 2)

    def test_foreign_comment_identity_is_rejected(self):
        wrong = {
            **comment(),
            "html_url": "https://github.com/other/repo/issues/7#issuecomment-1",
        }
        with self.responses((issue(comments=1), ""), ([wrong], "")):
            with self.assertRaises(ContextError):
                self.client.fetch(ITEM)

    def test_missing_page_count_is_rejected(self):
        with self.responses((issue(comments=2), ""), ([comment()], "")):
            with self.assertRaisesRegex(ContextError, "incomplete"):
                self.client.fetch(ITEM)

    def test_duplicate_comment_is_rejected(self):
        with self.responses((issue(comments=2), ""), ([comment(), comment()], "")):
            with self.assertRaisesRegex(ContextError, "Duplicate"):
                self.client.fetch(ITEM)

    def test_manual_context_does_not_make_an_issue_request(self):
        item = {**ITEM, "source": "manual"}
        with patch.object(self.client, "_get") as fetch:
            result = self.client.fetch(item)
        fetch.assert_not_called()
        self.assertEqual(result["title"], item["title"])
        self.assertEqual(result["instructions"], item["instructions"])

    def test_discussion_fingerprint_ignores_fetch_time_but_detects_body_change(self):
        first = manual_context({**ITEM, "source": "manual"}, fetched_at=NOW)
        second = {**first, "fetched_at": "2026-11-15T13:00:00Z"}
        self.assertEqual(discussion_digest(first), discussion_digest(second))
        changed = {**second, "instructions": "Changed discussion"}
        self.assertNotEqual(discussion_digest(first), discussion_digest(changed))

    def test_http_client_disables_redirects_and_sends_selected_credential(self):
        opener = unittest.mock.Mock()
        opener.open.return_value = Response(issue())
        self.client.opener = opener
        data, _ = self.client._get(
            "https://api.github.com/repos/original/issues/issues/7"
        )
        self.assertEqual(data["number"], 7)
        request = opener.open.call_args.args[0]
        self.assertEqual(
            request.get_header("Authorization"), "Bearer selected-issue-read-token"
        )

    def test_invalid_saved_comment_timestamp_is_rejected(self):
        with self.responses((issue(comments=1), ""), ([comment()], "")):
            result = self.client.fetch(ITEM)
        result = copy.deepcopy(result)
        result["comments"][0]["updated_at"] = "yesterday"
        with self.assertRaises(ContextError):
            validate_context(result, ITEM)

    def test_deeply_nested_response_fails_as_unreadable_context(self):
        response = Response({})
        response.seek(0)
        response.write(b"[" * 10000 + b"0" + b"]" * 10000)
        response.seek(0)
        opener = unittest.mock.Mock()
        opener.open.return_value = response
        self.client.opener = opener
        with self.assertRaises(ContextError):
            self.client._get("https://api.github.com/repos/original/issues/issues/7")
