import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import Request

from coding_runner.delivery import DeliveryError, DeliveryUnknown, GitHubDeliveryClient


class DeliveryHTTPTests(unittest.TestCase):
    def setUp(self):
        self.responses = []
        self.requests = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.respond()

            def do_POST(self):
                self.respond()

            def respond(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                owner.requests.append(
                    (self.command, self.path, self.headers.get("Authorization"), body)
                )
                status, payload, headers = owner.responses.pop(0)
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                for key, value in headers.items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.shutdown)
        self.client = GitHubDeliveryClient("test-private-repository-token")
        opener = self.client.opener
        endpoint = f"http://127.0.0.1:{server.server_port}"

        class TestTransport:
            def open(self, request, **kwargs):
                # Rewrite transport only: production URL/path validation and redirect policy remain exercised.
                target = request.full_url.replace("https://api.github.com", endpoint)
                return opener.open(
                    Request(
                        target,
                        data=request.data,
                        headers=dict(request.header_items()),
                        method=request.method,
                    ),
                    **kwargs,
                )

        self.client.opener = TestTransport()

    def test_real_http_lookup_uses_selected_credential_and_identity_query(self):
        self.responses.append((200, b"[]", {}))
        self.assertIsNone(self.client.find("example/repo", "nq/task", "main", "a" * 40))
        method, path, authorization, body = self.requests[0]
        self.assertEqual(method, "GET")
        self.assertIn("head=example%3Anq%2Ftask", path)
        self.assertEqual(authorization, "Bearer test-private-repository-token")
        self.assertEqual(body, b"")

    def test_real_http_redirect_does_not_forward_token(self):
        self.responses.append((302, b"{}", {"Location": "http://127.0.0.1:9/steal"}))
        with self.assertRaises(DeliveryError):
            self.client.find("example/repo", "nq/task", "main", "a" * 40)
        self.assertEqual(len(self.requests), 1)

    def test_mutation_server_error_requires_reconciliation(self):
        self.responses.append((503, b"{}", {}))
        with self.assertRaises(DeliveryUnknown):
            self.client.create(
                "example/repo", "nq/task", "main", "a" * 40, "Problem", "Evidence"
            )

    def test_invalid_successful_mutation_requires_reconciliation(self):
        self.responses.append((201, b"not json", {}))
        with self.assertRaises(DeliveryUnknown):
            self.client.create(
                "example/repo", "nq/task", "main", "a" * 40, "Problem", "Evidence"
            )

    def test_oversized_response_is_rejected(self):
        self.responses.append((200, json.dumps("x" * 1048576).encode(), {}))
        with self.assertRaises(DeliveryError):
            self.client.find("example/repo", "nq/task", "main", "a" * 40)


class RepositoryCredentialTests(unittest.TestCase):
    def test_helper_reads_private_descriptor_only_for_selected_repository(self):
        from coding_runner.workspaces import clean_environment
        from coding_runner import git_credential

        secret = "test-private-token-never-in-argv-or-environment"
        helper = str(Path(git_credential.__file__).resolve())
        with tempfile.TemporaryFile() as stream:
            stream.write(secret.encode())
            stream.flush()
            arguments = [
                sys.executable,
                "-I",
                helper,
                str(os.getpid()),
                str(stream.fileno()),
                "example/repo",
                "get",
            ]
            self.assertNotIn(secret, arguments)
            environment = clean_environment()
            self.assertNotIn(secret, environment.values())
            result = subprocess.run(
                arguments,
                input=b"protocol=https\nhost=github.com\npath=example/repo.git\n\n",
                env=environment,
                capture_output=True,
                check=True,
            )
            self.assertIn(("password=" + secret).encode(), result.stdout)
            denied = subprocess.run(
                arguments,
                input=b"protocol=https\nhost=github.com\npath=unrelated/repo\n\n",
                env=environment,
                capture_output=True,
                check=True,
            )
            self.assertEqual(denied.stdout, b"")
            denied_host = subprocess.run(
                arguments,
                input=b"protocol=https\nhost=other.example\npath=example/repo\n\n",
                env=environment,
                capture_output=True,
                check=True,
            )
            self.assertEqual(denied_host.stdout, b"")


if __name__ == "__main__":
    unittest.main()
