import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
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
    UnavailableError,
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
        if self.command == "POST" and hasattr(self.server, "release_mutation"):
            self.server.release_mutation.wait(2)
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
    def test_local_stop_bounds_inflight_http_mutation_and_preserves_lock(self):
        stopped = threading.Event()
        self.server.release_mutation = threading.Event()
        self.client.timeout = 15
        callback_threads = []

        def stop_after_request():
            deadline = time.monotonic() + 2
            while not self.server.calls and time.monotonic() < deadline:
                time.sleep(0.005)
            stopped.set()

        def checkpoint():
            callback_threads.append(threading.get_ident())
            return not stopped.is_set()

        stopper = threading.Thread(target=stop_after_request, daemon=True)
        stopper.start()
        try:
            started = time.monotonic()
            with self.assertRaises(AmbiguousRequest):
                self.client.request(
                    "POST", "/api/v1/runner/runs/claim/", {}, checkpoint=checkpoint
                )
            self.assertLess(time.monotonic() - started, 0.7)
            with self.assertRaises(UnavailableError):
                self.client.identity()
            self.assertEqual(
                self.server.calls, [("POST", "/api/v1/runner/runs/claim/")]
            )
        finally:
            self.server.release_mutation.set()
            stopper.join(2)
        self.assertTrue(self.client.request_lock.acquire(timeout=2))
        self.client.request_lock.release()
        self.assertEqual(set(callback_threads), {threading.get_ident()})

    def test_durable_interrupted_mutation_reconciles_exact_pending_request_over_http(
        self,
    ):
        from coding_runner.recovery import DurableClient

        run_id, operation = str(uuid4()), str(uuid4())
        path = f"/api/v1/runner/runs/{run_id}/contexts/"
        lookup = f"/api/v1/runner/runs/{run_id}/operations/{operation}/"
        payload = {
            "operation_id": operation,
            "claim_token": "private-test-claim",
            "document": {"title": "original"},
        }
        response = {"context": {"id": str(uuid4())}}
        self.server.responses[path] = (200, response)
        self.server.responses[lookup] = (200, {"response": response})
        self.server.release_mutation = threading.Event()
        self.client.timeout = 15
        stopped = threading.Event()

        def stop_after_send():
            deadline = time.monotonic() + 2
            while not self.server.calls and time.monotonic() < deadline:
                time.sleep(0.005)
            stopped.set()

        stopper = threading.Thread(target=stop_after_send, daemon=True)
        with Journal(self.path) as journal:
            state = journal.load()
            durable = DurableClient(self.client, journal, state)
            try:
                stopper.start()
                started = time.monotonic()
                with self.assertRaises(AmbiguousRequest):
                    durable.request(
                        "POST",
                        path,
                        payload,
                        timeout=2,
                        checkpoint=lambda: not stopped.is_set(),
                    )
                self.assertLess(time.monotonic() - started, 0.7)
                pending = journal.load()["request"]
                self.assertEqual(
                    pending, {"path": path, "payload": payload, "status": "pending"}
                )
            finally:
                self.server.release_mutation.set()
                stopper.join(2)
            self.assertTrue(self.client.request_lock.acquire(timeout=2))
            self.client.request_lock.release()
            restored = journal.load()
            DurableClient(self.client, journal, restored).recover(
                timeout=1, checkpoint=lambda: True
            )
            self.assertEqual(restored["request"]["response"], response)
        self.assertEqual(self.server.calls, [("POST", path), ("GET", lookup)])

    def test_full_handoff_gates_can_use_same_client_between_requests(self):
        from unittest.mock import Mock
        from coding_runner.plan_store import ContextPlanStore
        from coding_runner.test_context import ITEM
        from coding_runner.test_plans import BASE, saved_context

        run_id = str(uuid4())
        context = saved_context()
        result = {"context": context, "plan": {"id": str(uuid4())}}
        context_path = f"/api/v1/runner/runs/{run_id}/contexts/"
        handoff_path = f"/api/v1/runner/runs/{run_id}/handoff/"
        self.server.responses[context_path] = (200, {"context": context})
        self.server.responses[handoff_path] = (200, result)
        store = ContextPlanStore(self.client, run_id, str(uuid4()), Mock())

        def full_gate():
            return self.client.identity() == IDENTITY

        actual = store.handoff(
            {**ITEM, "source": "manual"},
            BASE,
            timeout_seconds=2,
            checkpoint=full_gate,
            inflight_checkpoint=lambda: True,
        )
        self.assertEqual(actual, result)
        mutations = [path for method, path in self.server.calls if method == "POST"]
        self.assertEqual(mutations, [context_path, handoff_path])

    def test_per_request_timeout_cannot_extend_the_configured_maximum(self):
        release = threading.Event()

        def stalled(*args, **kwargs):
            release.wait(2)
            raise TimeoutError

        try:
            with patch.object(self.client.opener, "open", side_effect=stalled):
                started = time.monotonic()
                with self.assertRaises(UnavailableError):
                    self.client.request("GET", "/api/v1/runner/identity/", timeout=60)
                self.assertLess(time.monotonic() - started, 0.7)
        finally:
            release.set()
        self.assertTrue(self.client.request_lock.acquire(timeout=2))
        self.client.request_lock.release()

    def test_invalid_per_request_timeout_never_starts_transport(self):
        with patch.object(self.client.opener, "open") as transport:
            for timeout in [0, -1, float("nan")]:
                with self.assertRaises(ValueError):
                    self.client.request(
                        "GET", "/api/v1/runner/identity/", timeout=timeout
                    )
        transport.assert_not_called()

    def test_request_timeout_caps_the_normal_client_deadline(self):
        self.client.timeout = 15
        release = threading.Event()

        def stalled(*args, **kwargs):
            release.wait(2)
            raise TimeoutError

        try:
            with patch.object(self.client.opener, "open", side_effect=stalled):
                started = time.monotonic()
                with self.assertRaises(UnavailableError):
                    self.client.request("GET", "/api/v1/runner/identity/", timeout=0.1)
                self.assertLess(time.monotonic() - started, 0.7)
                with self.assertRaises(UnavailableError):
                    self.client.identity()
                self.assertEqual(self.client.opener.open.call_count, 1)
        finally:
            release.set()
        self.assertTrue(self.client.request_lock.acquire(timeout=2))
        self.client.request_lock.release()

    def test_slow_headers_cannot_extend_request_deadline(self):
        class SlowHeaders(BaseHTTPRequestHandler):
            def do_GET(self):
                self.request.sendall(b"HTTP/1.0 200 OK\r\nX-Slow: ")
                try:
                    for _ in range(20):
                        self.request.sendall(b"x")
                        time.sleep(0.05)
                except OSError:
                    pass

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), SlowHeaders)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            client = Client(
                f"http://127.0.0.1:{server.server_port}", "test", timeout=0.2
            )
            started = time.monotonic()
            with self.assertRaises(UnavailableError):
                client.identity()
            self.assertLess(time.monotonic() - started, 0.7)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_stalled_transport_blocks_overlapping_requests(self):
        release = threading.Event()

        def stalled(*args, **kwargs):
            release.wait()
            raise TimeoutError

        try:
            with patch.object(self.client.opener, "open", side_effect=stalled):
                with self.assertRaises(AmbiguousRequest):
                    self.client.request("POST", "/api/v1/runner/runs/claim/", {})
                with self.assertRaises(UnavailableError):
                    self.client.identity()
                self.assertEqual(self.client.opener.open.call_count, 1)
        finally:
            release.set()

    def test_local_stop_after_mutation_transport_is_ambiguous(self):
        stopped = threading.Event()
        original = self.client.opener.open

        def open_response(*args, **kwargs):
            response = original(*args, **kwargs)
            stopped.set()
            return response

        with patch.object(self.client.opener, "open", side_effect=open_response):
            with self.assertRaises(AmbiguousRequest):
                self.client.request(
                    "POST",
                    "/api/v1/runner/runs/claim/",
                    {},
                    checkpoint=lambda: not stopped.is_set(),
                )
        self.assertEqual(len(self.server.calls), 1)
        with self.assertRaises(UnavailableError):
            self.client.request(
                "POST",
                "/api/v1/runner/runs/claim/",
                {},
                checkpoint=lambda: not stopped.is_set(),
            )
        self.assertEqual(len(self.server.calls), 1)

    def test_local_stop_after_read_transport_is_unavailable(self):
        stopped = threading.Event()
        original = self.client.opener.open

        def open_response(*args, **kwargs):
            response = original(*args, **kwargs)
            stopped.set()
            return response

        with patch.object(self.client.opener, "open", side_effect=open_response):
            with self.assertRaises(UnavailableError):
                self.client.request(
                    "GET",
                    "/api/v1/runner/identity/",
                    checkpoint=lambda: not stopped.is_set(),
                )
        self.assertEqual(len(self.server.calls), 1)

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

    def test_excessively_nested_mutation_response_is_ambiguous(self):
        self.server.responses["/api/v1/runner/runs/claim/"] = (
            200,
            b'{"data":' + b"[" * 100000 + b"0" + b"]" * 100000 + b"}",
        )
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

    def test_shutdown_does_not_start_another_probe_request(self):
        stop = threading.Event()
        identity = self.client.identity

        def stop_after_identity():
            response = identity()
            stop.set()
            return response

        with Journal(self.path) as journal:
            with patch.object(self.client, "identity", side_effect=stop_after_identity):
                Controller(self.client, journal, stop).run(check=True)
        self.assertEqual(self.server.calls, [("GET", "/api/v1/runner/identity/")])

    def test_shutdown_does_not_start_pending_reconciliation(self):
        stop = threading.Event()
        active = self.client.active

        def stop_after_active():
            response = active()
            stop.set()
            return response

        with Journal(self.path) as journal:
            journal.prepare(journal.load(), str(uuid4()), "claim", {})
            with patch.object(self.client, "active", side_effect=stop_after_active):
                Controller(self.client, journal, stop).run(check=True)
            self.assertEqual(journal.load()["operation"]["status"], "pending")
        self.assertEqual(len(self.server.calls), 3)

    def test_restart_reconciles_pending_mutation_without_repeating_it(self):
        run_id = str(uuid4())
        path = f"/api/v1/runner/runs/{run_id}/"
        self.server.responses[path] = (200, {"run": {"state": "active"}})
        with Journal(self.path) as journal:
            state = journal.load()
            state["controller_secret"] = "a" * 64
            journal.prepare(state, run_id, "claim", {"run_id": run_id})
        with Journal(self.path) as journal:
            Controller(self.client, journal).run(check=True)
            self.assertEqual(self.client.controller_secret, "a" * 64)
            self.assertEqual(
                journal.load()["operation"]["reconciliation"]["run"]["state"], "active"
            )
        self.assertTrue(all(method == "GET" for method, _ in self.server.calls))

    def test_restart_with_absent_claim_rechecks_current_pause_over_http(self):
        import copy
        from coding_runner.gates import GateEvaluator
        from coding_runner.recovery import RecoveryCoordinator, ReconciliationRequired
        from coding_runner.test_gates import CONFIG as GATE_CONFIG, NOW

        run_id = str(uuid4())
        configuration = copy.deepcopy(GATE_CONFIG)
        self.server.responses[f"/api/v1/runner/runs/{run_id}/"] = (404, {})
        with Journal(self.path) as journal:
            state = journal.load()
            gates = GateEvaluator(journal, clock=lambda: NOW)
            decision = gates.evaluate(configuration, state)
            gates.prepare_claim(state, run_id, decision)
            payload = {
                "run_id": run_id,
                "configuration_revision": configuration["revision"],
            }
            journal.prepare(state, run_id, "claim", payload)
            configuration["global"]["paused"] = True
            self.server.responses["/api/v1/runner/configuration/"] = (
                200,
                configuration,
            )
            with self.assertRaisesRegex(ReconciliationRequired, "processing gates"):
                RecoveryCoordinator(self.client, journal, gates).reconcile(
                    journal.load()
                )
            self.assertEqual(journal.load()["operation"]["payload"], payload)
        self.assertEqual(
            self.server.calls,
            [
                ("GET", f"/api/v1/runner/runs/{run_id}/"),
                ("GET", "/api/v1/runner/configuration/"),
                ("POST", "/api/v1/runner/gates/report/"),
            ],
        )

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

    def test_late_mutation_response_does_not_clear_pending_journal(self):
        self.server.release_mutation = threading.Event()
        run_id = str(uuid4())
        try:
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
                        {"run_id": run_id},
                    )
                self.server.release_mutation.set()
                self.assertTrue(self.client.request_lock.acquire(timeout=1))
                self.client.request_lock.release()
                self.assertEqual(journal.load()["operation"]["status"], "pending")
        finally:
            self.server.release_mutation.set()
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
