import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from .http import APIError, GitHubClient, QueueClient, request_json


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_GET(self):
        self.server.requests.append(self.path)
        self.server.authorization = self.headers.get("Authorization")
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/leaked")
            self.end_headers()
            return
        self.send_response(200)
        self.end_headers()
        self.wfile.write(json.dumps({"projects": []}).encode())

    def do_POST(self):
        self.server.requests.append(self.path)
        size = int(self.headers["Content-Length"])
        self.server.body = json.loads(self.rfile.read(size))
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"item": null}')


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.requests = []
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def test_queue_checks_authentication_without_claiming(self):
        QueueClient(self.url, "private-token").check()
        self.assertEqual(self.server.requests, ["/api/v1/projects/"])
        self.assertEqual(self.server.authorization, "Bearer private-token")

    def test_redirects_do_not_forward_credentials(self):
        with self.assertRaises(APIError):
            request_json(self.url + "/redirect", "private-token")
        self.assertEqual(self.server.requests, ["/redirect"])

    def test_claim_passes_execution_reference_as_json(self):
        self.assertIsNone(QueueClient(self.url, "token").claim("run-id"))
        self.assertEqual(self.server.body, {"execution_ref": "runner:run-id"})

    def test_null_completion_response_is_not_accepted(self):
        item = {"id": 1, "claim_token": "private-token"}
        with self.assertRaises(APIError):
            QueueClient(self.url, "token").finish(item, {"status": "failed"})

    @patch("coding_runner.http.request_json")
    def test_existing_open_pr_is_reused(self, request):
        request.return_value = [
            {
                "head": {"ref": "nq/1"},
                "base": {"ref": "main"},
                "state": "open",
                "html_url": "https://github.com/example/repo/pull/1",
            }
        ]
        result = GitHubClient("token").pull_request(
            "example/repo", "nq/1", "main", "Task", "Body"
        )
        self.assertEqual(result, "https://github.com/example/repo/pull/1")
        self.assertEqual(request.call_count, 1)

    @patch("coding_runner.http.request_json")
    def test_closed_pr_is_not_silently_recreated(self, request):
        request.return_value = [
            {"head": {"ref": "nq/1"}, "base": {"ref": "main"}, "state": "closed"}
        ]
        with self.assertRaises(APIError):
            GitHubClient("token").pull_request(
                "example/repo", "nq/1", "main", "Task", "Body"
            )
        self.assertEqual(request.call_count, 1)

    @patch("coding_runner.http.request_json")
    def test_new_pr_uses_resolved_base_branch(self, request):
        request.side_effect = [
            [],
            {"html_url": "https://github.com/example/repo/pull/1"},
        ]
        GitHubClient("token").pull_request(
            "example/repo", "nq/1", "develop", "Task", "Body"
        )
        self.assertEqual(request.call_args.args[2]["base"], "develop")
