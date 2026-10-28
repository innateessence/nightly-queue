import argparse
import io
import json
from contextlib import redirect_stderr, redirect_stdout
from unittest import TestCase
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from nq.cli import NoRedirects, main, positive_id, request_api, validate_server_url


class CLITests(TestCase):
    def setUp(self):
        values = {"NQ_API_TOKEN": "test-token", "NQ_URL": "http://localhost:8000"}
        environment = patch.dict("os.environ", values, clear=True)
        environment.start()
        self.addCleanup(environment.stop)

    def run_cli(self, argv):
        output = io.StringIO()
        errors = io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            status = main(argv)
        return status, output.getvalue(), errors.getvalue()

    def assert_usage_error(self, argv):
        with self.assertRaises(SystemExit) as raised:
            self.run_cli(argv)
        self.assertEqual(raised.exception.code, 2)

    @patch("nq.cli.request_api")
    def test_add_sends_project_and_instructions(self, request):
        item = {"id": 42, "project": {"id": 12}, "title": "Add export"}
        request.return_value = {"item": item}
        argv = ["add", "--project", "12", "Add export", "--instructions", "CSV"]
        self.run_cli(argv)
        expected = {
            "project_id": 12,
            "title": "Add export",
            "instructions": "CSV",
            "priority": "normal",
        }
        url = "http://localhost:8000"
        request.assert_called_once_with(url, "test-token", "items/", expected)

    @patch("nq.cli.request_api")
    def test_add_reports_created_item(self, request):
        item = {"id": 42, "project": {"id": 12}, "title": "Add export"}
        request.return_value = {"item": item}
        status, output, errors = self.run_cli(["add", "--project", "12", "Add export"])
        self.assertEqual(status, 0)
        self.assertIn("Queued item #42 for project #12: Add export", output)
        self.assertEqual(errors, "")

    @patch("nq.cli.request_api")
    def test_projects_lists_existing_ids(self, request):
        project = {
            "id": 12,
            "name": "Example",
            "repository_url": "https://github.com/a/b.git",
        }
        request.return_value = {"projects": [project]}
        status, output, _ = self.run_cli(["projects"])
        self.assertEqual(status, 0)
        self.assertIn("12\tExample\thttps://github.com/a/b.git", output)

    @patch("nq.cli.request_api")
    def test_projects_explains_empty_list(self, request):
        request.return_value = {"projects": []}
        status, output, _ = self.run_cli(["projects"])
        self.assertEqual(status, 0)
        self.assertIn("Create one in Django admin", output)

    def test_add_requires_project_argument(self):
        self.assert_usage_error(["add", "Task"])

    def test_add_rejects_negative_project_id(self):
        self.assert_usage_error(["add", "Task", "--project", "-1"])

    def test_add_rejects_non_numeric_project_id(self):
        self.assert_usage_error(["add", "Task", "--project", "no"])

    def test_missing_token_exits_nonzero(self):
        with patch.dict("os.environ", {"NQ_API_TOKEN": ""}):
            status, _, errors = self.run_cli(["projects"])
        self.assertEqual(status, 1)
        self.assertIn("set NQ_API_TOKEN", errors)

    def test_invalid_server_url_exits_nonzero(self):
        with patch.dict("os.environ", {"NQ_URL": "file:///tmp/db"}):
            status, _, errors = self.run_cli(["projects"])
        self.assertEqual(status, 1)
        self.assertIn("NQ_URL must be an HTTP(S) server URL", errors)

    @patch("nq.cli.request_api")
    def test_server_validation_error_exits_nonzero(self, request):
        request.side_effect = RuntimeError("API returned HTTP 400: Unknown project")
        status, _, errors = self.run_cli(["add", "Task", "--project", "12"])
        self.assertEqual(status, 1)
        self.assertIn("Unknown project", errors)

    @patch("nq.cli.request_api")
    def test_unexpected_response_exits_nonzero(self, request):
        request.return_value = {}
        status, _, errors = self.run_cli(["projects"])
        self.assertEqual(status, 1)
        self.assertIn("unexpected response", errors)


class HTTPClientTests(TestCase):
    def setUp(self):
        opener_patch = patch("nq.cli.build_opener")
        build_opener = opener_patch.start()
        self.addCleanup(opener_patch.stop)
        self.opener = build_opener.return_value
        self.opener.open.return_value = io.BytesIO(b'{"projects": []}')

    def request(self, data=None):
        return request_api("http://localhost:8000/", "test-token", "projects/", data)

    def test_request_uses_api_url(self):
        self.request()
        request = self.opener.open.call_args.args[0]
        self.assertEqual(request.full_url, "http://localhost:8000/api/v1/projects/")

    def test_request_sends_bearer_token(self):
        self.request()
        request = self.opener.open.call_args.args[0]
        self.assertEqual(request.get_header("Authorization"), "Bearer test-token")

    def test_request_has_timeout(self):
        self.request()
        self.assertEqual(self.opener.open.call_args.kwargs["timeout"], 30)

    def test_request_encodes_json_body(self):
        self.request({"title": "Work"})
        request = self.opener.open.call_args.args[0]
        self.assertEqual(json.loads(request.data), {"title": "Work"})
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("Content-type"), "application/json")

    def test_request_returns_json_object(self):
        self.assertEqual(self.request(), {"projects": []})

    def test_http_error_reports_json_details(self):
        body = io.BytesIO(b'{"errors": {"project_id": ["Unknown"]}}')
        error = HTTPError("http://localhost", 400, "Bad Request", {}, body)
        self.opener.open.side_effect = error
        with self.assertRaisesRegex(RuntimeError, "HTTP 400.*Unknown"):
            self.request()

    def test_http_error_falls_back_to_reason(self):
        body = io.BytesIO(b"not json")
        error = HTTPError("http://localhost", 502, "Bad Gateway", {}, body)
        self.opener.open.side_effect = error
        with self.assertRaisesRegex(RuntimeError, "HTTP 502: Bad Gateway"):
            self.request()

    def test_connection_error_reports_reason(self):
        self.opener.open.side_effect = URLError("Connection refused")
        message = "Could not reach.*Connection refused"
        with self.assertRaisesRegex(RuntimeError, message):
            self.request()

    def test_timeout_reports_failure(self):
        self.opener.open.side_effect = TimeoutError("timed out")
        with self.assertRaisesRegex(RuntimeError, "Could not reach.*timed out"):
            self.request()

    def test_invalid_json_reports_failure(self):
        self.opener.open.return_value = io.BytesIO(b"not json")
        with self.assertRaisesRegex(RuntimeError, "invalid JSON"):
            self.request()

    def test_non_object_response_reports_failure(self):
        self.opener.open.return_value = io.BytesIO(b"[]")
        with self.assertRaisesRegex(RuntimeError, "invalid JSON"):
            self.request()

    def test_redirects_are_not_followed(self):
        handler = NoRedirects()
        url = "https://elsewhere"
        result = handler.redirect_request(None, None, 302, "Found", {}, url)
        self.assertIsNone(result)


class CLIValidationTests(TestCase):
    def test_positive_project_id_is_parsed(self):
        self.assertEqual(positive_id("12"), 12)

    def test_zero_project_id_is_rejected(self):
        with self.assertRaises(argparse.ArgumentTypeError):
            positive_id("0")

    def test_out_of_range_project_id_is_rejected(self):
        with self.assertRaises(argparse.ArgumentTypeError):
            positive_id(str(2**63))

    def test_http_server_url_is_accepted(self):
        self.assertIsNone(validate_server_url("http://localhost:8000"))

    def test_https_server_url_is_accepted(self):
        self.assertIsNone(validate_server_url("https://example.com"))

    def test_server_url_rejects_credentials(self):
        with self.assertRaises(ValueError):
            validate_server_url("https://user:password@example.com")

    def test_server_url_rejects_query(self):
        with self.assertRaises(ValueError):
            validate_server_url("https://example.com?token=secret")

    def test_server_url_rejects_fragment(self):
        with self.assertRaises(ValueError):
            validate_server_url("https://example.com#fragment")

    def test_server_url_rejects_whitespace(self):
        with self.assertRaises(ValueError):
            validate_server_url("https://example.com/a b")

    def test_server_url_rejects_out_of_range_port(self):
        with self.assertRaises(ValueError):
            validate_server_url("https://example.com:99999")
