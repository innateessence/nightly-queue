import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import TestCase
from unittest.mock import patch

from .context import ContextError, GitHubContextClient
from .test_context import Response, issue


class ContextDeadlineTests(TestCase):
    def test_stalled_dns_is_bounded_and_prevents_overlapping_workers(self):
        client = GitHubContextClient("local-test-credential", timeout=0.1)
        started = threading.Event()
        release = threading.Event()

        def stalled_dns(*args, **kwargs):
            started.set()
            release.wait(2)
            raise OSError("local-test-credential must not escape")

        try:
            with patch("socket.getaddrinfo", side_effect=stalled_dns) as resolver:
                before = time.monotonic()
                with self.assertRaises(ContextError) as error:
                    client._get("https://api.github.com/repos/example/repo/issues/1")
                self.assertTrue(started.is_set())
                self.assertLess(time.monotonic() - before, 0.7)
                self.assertNotIn(client.token, str(error.exception))
                for _ in range(3):
                    with self.assertRaises(ContextError):
                        client._get(
                            "https://api.github.com/repos/example/repo/issues/1"
                        )
                self.assertEqual(resolver.call_count, 1)
        finally:
            release.set()
        self.assertTrue(client.request_lock.acquire(timeout=2))
        client.request_lock.release()
        with patch.object(client.opener, "open", return_value=Response(issue())):
            data, _ = client._get("https://api.github.com/repos/example/repo/issues/1")
        self.assertEqual(data["number"], 7)

    def test_dripping_headers_cannot_extend_the_absolute_deadline(self):
        release = threading.Event()

        class DrippingHeaders(BaseHTTPRequestHandler):
            def do_GET(self):
                self.request.sendall(b"HTTP/1.0 200 OK\r\nX-Slow: ")
                try:
                    for _ in range(100):
                        if release.wait(0.02):
                            break
                        self.request.sendall(b"x")
                except OSError:
                    pass

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), DrippingHeaders)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        client = GitHubContextClient("local-test-credential", timeout=0.1)
        try:
            before = time.monotonic()
            with self.assertRaises(ContextError):
                client._get(f"http://127.0.0.1:{server.server_port}/local-fixture")
            self.assertLess(time.monotonic() - before, 0.7)
            with self.assertRaises(ContextError):
                client._get(f"http://127.0.0.1:{server.server_port}/local-fixture")
        finally:
            release.set()
            server.shutdown()
            server.server_close()
            thread.join()
        self.assertTrue(client.request_lock.acquire(timeout=2))
        client.request_lock.release()

    def test_redirect_response_is_not_followed_with_the_credential(self):
        paths = []

        class Redirect(BaseHTTPRequestHandler):
            def do_GET(self):
                paths.append(self.path)
                self.send_response(302)
                self.send_header("Location", "/credential-sink")
                self.end_headers()

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Redirect)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        client = GitHubContextClient("local-test-credential", timeout=0.2)
        try:
            with self.assertRaises(ContextError) as error:
                client._get(f"http://127.0.0.1:{server.server_port}/issue")
            self.assertNotIn(client.token, str(error.exception))
            self.assertEqual(paths, ["/issue"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_expired_aggregate_deadline_never_starts_a_request(self):
        client = GitHubContextClient("local-test-credential")
        with patch.object(client.opener, "open") as transport:
            with self.assertRaises(ContextError):
                client._get(
                    "https://api.github.com/repos/example/repo/issues/1",
                    deadline=time.monotonic() - 1,
                )
        transport.assert_not_called()

    def test_aggregate_deadline_caps_the_normal_request_limit(self):
        client = GitHubContextClient("local-test-credential", timeout=15)
        release = threading.Event()

        def stalled(*args, **kwargs):
            release.wait(2)
            raise OSError

        try:
            with patch.object(client.opener, "open", side_effect=stalled):
                before = time.monotonic()
                with self.assertRaises(ContextError):
                    client._get(
                        "https://api.github.com/repos/example/repo/issues/1",
                        deadline=before + 0.1,
                    )
                self.assertLess(time.monotonic() - before, 0.7)
        finally:
            release.set()
        self.assertTrue(client.request_lock.acquire(timeout=2))
        client.request_lock.release()
