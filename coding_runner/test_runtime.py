import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import TestCase, main
from unittest.mock import patch
from uuid import uuid4

from coding_runner.client import (
    APIError,
    AmbiguousRequest,
    AuthenticationError,
    Client,
    ConflictError,
    InvalidResponse,
)
from coding_runner.controller import Controller, journaled_request
from coding_runner.journal import ControllerLocked, Journal, JournalError

IDENTITY = {"schema_version": 1, "runner": {"id": 7, "name": "test"}}
CONFIG = {
    "schema_version": 1,
    "revision": "test-revision",
    "global": {"polling_interval_seconds": 1},
}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.server.calls.append((self.command, self.path))
        data = self.server.responses.get(self.path, (200, IDENTITY))
        status, body = data
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        encoded = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.wfile.write(encoded)

    do_POST = do_GET

    def log_message(self, *args):
        pass


class ServerCase(TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.calls = []
        self.server.responses = {
            "/api/v1/runner/configuration/": (200, CONFIG),
            "/api/v1/runner/runs/active/": (200, {"run": None, "item": None}),
        }
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.client = Client(
            f"http://127.0.0.1:{self.server.server_port}", "test-token", timeout=0.2
        )
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "state"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temporary.cleanup()


class ClientTests(ServerCase):
    def test_authentication_error_is_explicit_and_redacted(self):
        self.server.responses["/api/v1/runner/identity/"] = (401, {"error": "secret"})
        with self.assertRaises(AuthenticationError) as error:
            self.client.identity()
        self.assertNotIn("secret", str(error.exception))

    def test_conflict_does_not_retry(self):
        self.server.responses["/api/v1/runner/runs/claim/"] = (409, {})
        with self.assertRaises(ConflictError):
            self.client.request("POST", "/api/v1/runner/runs/claim/", {})
        self.assertEqual(len(self.server.calls), 1)

    def test_invalid_read_response_is_explicit(self):
        self.server.responses["/api/v1/runner/identity/"] = (
            200,
            b"<html>invalid</html>",
        )
        with self.assertRaises(InvalidResponse):
            self.client.identity()

    def test_response_size_is_bounded(self):
        self.client.max_bytes = 10
        with self.assertRaises(InvalidResponse):
            self.client.identity()

    def test_invalid_mutation_response_is_ambiguous(self):
        self.server.responses["/api/v1/runner/runs/claim/"] = (200, b"invalid")
        with self.assertRaises(AmbiguousRequest):
            self.client.request("POST", "/api/v1/runner/runs/claim/", {})

    def test_server_error_after_mutation_is_ambiguous(self):
        self.server.responses["/api/v1/runner/runs/claim/"] = (503, {})
        with self.assertRaises(AmbiguousRequest):
            self.client.request("POST", "/api/v1/runner/runs/claim/", {})

    def test_timeout_after_mutation_is_ambiguous(self):
        with patch.object(self.client.opener, "open", side_effect=TimeoutError):
            with self.assertRaises(AmbiguousRequest):
                self.client.request("POST", "/api/v1/runner/runs/claim/", {})

    def test_redirect_does_not_forward_token(self):
        self.server.responses["/api/v1/runner/identity/"] = (302, {})
        with self.assertRaises(APIError):
            self.client.identity()
        self.assertEqual(len(self.server.calls), 1)

    def test_check_only_authenticates_and_reads_configuration(self):
        with Journal(self.path) as journal:
            Controller(self.client, journal).run(check=True)
        calls = [
            ("GET", "/api/v1/runner/identity/"),
            ("GET", "/api/v1/runner/configuration/"),
            ("GET", "/api/v1/runner/runs/active/"),
        ]
        self.assertEqual(self.server.calls, calls)

    def test_restart_reconciles_pending_mutation_without_repeating_it(self):
        run_id = str(uuid4())
        path = f"/api/v1/runner/runs/{run_id}/"
        self.server.responses[path] = (200, {"run": {"state": "active"}})
        with Journal(self.path) as journal:
            state = journal.load()
            journal.prepare(state, run_id, "claim", {"run_id": run_id})
        with Journal(self.path) as journal:
            Controller(self.client, journal).run(check=True)
            self.assertEqual(
                journal.load()["operation"]["reconciliation"]["run"]["state"], "active"
            )
        self.assertTrue(all(method == "GET" for method, _ in self.server.calls))

    def test_pending_claim_is_written_before_http(self):
        run_id = str(uuid4())
        self.server.responses["/api/v1/runner/runs/claim/"] = (500, {})
        with Journal(self.path) as journal:
            state = journal.load()
            with self.assertRaises(AmbiguousRequest):
                journaled_request(
                    self.client,
                    journal,
                    state,
                    run_id,
                    "claim",
                    "/api/v1/runner/runs/claim/",
                    {},
                )
            self.assertEqual(journal.load()["operation"]["run_id"], run_id)
            with self.assertRaises(JournalError):
                journaled_request(
                    self.client,
                    journal,
                    state,
                    str(uuid4()),
                    "claim",
                    "/api/v1/runner/runs/claim/",
                    {},
                )
        self.assertEqual(len(self.server.calls), 1)

    def test_missing_journal_discovers_owned_remote_run(self):
        run_id = str(uuid4())
        self.server.responses["/api/v1/runner/runs/active/"] = (
            200,
            {"run": {"id": run_id}, "item": {"id": 42}},
        )
        with Journal(self.path) as journal:
            Controller(self.client, journal).run(check=True)
            self.assertEqual(journal.load()["remote_active_run_id"], run_id)
        self.assertTrue(all(method == "GET" for method, _ in self.server.calls))

    def test_changed_identity_cannot_reuse_journal(self):
        with Journal(self.path) as journal:
            journal.save({"version": 1, "runner_id": 99, "operation": None})
            with self.assertRaises(JournalError):
                Controller(self.client, journal).run(check=True)

    def test_unavailable_queue_backs_off_without_claiming(self):
        stop = threading.Event()
        waits = []

        def wait(delay):
            waits.append(delay)
            if len(waits) == 8:
                stop.set()

        with Journal(self.path) as journal:
            with patch.object(self.client.opener, "open", side_effect=TimeoutError):
                with patch.object(stop, "wait", side_effect=wait):
                    Controller(self.client, journal, stop).run()
        self.assertEqual(waits, [1, 2, 4, 8, 16, 32, 60, 60])
        self.assertEqual(self.server.calls, [])


class JournalTests(TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "state"

    def tearDown(self):
        self.temporary.cleanup()

    def test_state_survives_restart_with_private_permissions(self):
        state = {
            "version": 1,
            "runner_id": 7,
            "operation": None,
            "claim_token": "private",
        }
        with Journal(self.path) as journal:
            journal.save(state)
        with Journal(self.path) as journal:
            self.assertEqual(journal.load(), state)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o700)
        self.assertEqual((self.path / "journal.json").stat().st_mode & 0o777, 0o600)

    def test_second_controller_is_rejected(self):
        with Journal(self.path):
            with self.assertRaises(ControllerLocked):
                with Journal(self.path):
                    pass

    def test_failed_atomic_replace_preserves_previous_state(self):
        state = {"version": 1, "runner_id": 7, "operation": None}
        with Journal(self.path) as journal:
            journal.save(state)
            with patch("coding_runner.journal.os.replace", side_effect=OSError):
                with self.assertRaises(OSError):
                    journal.save({**state, "runner_id": 99})
            self.assertEqual(journal.load(), state)
        self.assertEqual(list(self.path.glob(".journal-*")), [])

    def test_corrupt_journal_fails_closed(self):
        with Journal(self.path) as journal:
            journal.path.write_text("incomplete")
            journal.path.chmod(0o600)
            with self.assertRaises(JournalError):
                journal.load()

    def test_public_directory_is_rejected(self):
        self.path.mkdir(mode=0o755)
        with self.assertRaises(JournalError):
            Journal(self.path)

    def test_symlinked_state_directory_is_rejected(self):
        self.path.symlink_to(self.temporary.name)
        with self.assertRaises(JournalError):
            Journal(self.path)

    def test_unlocked_state_is_rejected(self):
        with self.assertRaises(JournalError):
            Journal(self.path).load()

    def test_sigterm_stops_controller_cleanly_and_releases_lock(self):
        environment = {
            **os.environ,
            "NQ_URL": "http://127.0.0.1:1",
            "NQ_RUNNER_API_TOKEN": "test",
        }
        environment["NQ_RUNNER_STATE_DIR"] = str(self.path)
        process = subprocess.Popen(
            [sys.executable, "-m", "coding_runner"],
            env=environment,
            stderr=subprocess.PIPE,
        )
        try:
            self.assertIn(b"retrying", process.stderr.readline())
            process.terminate()
            self.assertEqual(process.wait(timeout=3), 0)
            with Journal(self.path):
                pass
        finally:
            process.kill() if process.poll() is None else None
            process.communicate()


if __name__ == "__main__":
    main()
