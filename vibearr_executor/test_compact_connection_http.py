import json
import socket
import threading
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from uuid import uuid4

from vibearr_runner.compact_connections import (
    CompactClient,
    CompactConnections,
    ConnectionServer,
    MediationClient,
)
from vibearr_runner.native_sessions import NativeBackendRegistry
from vibearr_runner.native_cli import NativeCLIAdapter
from vibearr_runner.policy import default_execution_policy

from .client import Client
from .compact_connections import WorkerConnections
from .compact_worker import CompactHandler
from .contracts import Conflict, Rejected, canonical
from .managed_login import install_login
from .proxy import Handler, Proxy, resolve_public
from .slot_http import SlotServer
from . import test_compact_connections as connection_fixtures
from . import test_compact_worker as worker_fixtures

login_files = connection_fixtures.login_files


class CompactAdapterRoutingTests(unittest.TestCase):
    def test_compact_client_selects_native_cli_without_integrated_identity(self):
        with TemporaryDirectory() as directory:
            client = CompactClient(
                object(), "http://runner:8792", "c" * 48, directory=directory
            )
            for provider in ("codex", "claude", "opencode"):
                with self.subTest(provider=provider):
                    adapter = NativeBackendRegistry().create(provider, client)
                    self.assertIs(type(adapter), NativeCLIAdapter)
                    self.assertEqual(adapter.provider, provider)
            self.assertIs(client.compact, True)
            self.assertIs(client.integrated, False)


class CompactConnectionHTTPTests(unittest.TestCase):
    setUp = worker_fixtures.CompactWorkerTests.setUp
    report = worker_fixtures.CompactWorkerTests.report

    def serve(self, server):
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_port}"

    def stack(self):
        self.report()
        worker_server = SlotServer(("127.0.0.1", 0), CompactHandler)
        worker_server.runtime, worker_server.token = self.runtime, "w" * 48
        with patch.dict(
            "os.environ", {"VIBEARR_RUNTIME_PROTOCOL": "compact-worker-v1"}
        ):
            worker = Client(self.serve(worker_server), worker_server.token)
        manager = CompactConnections(
            self.root / "runner/connections",
            worker,
            login_root=self.root / "runner/logins",
        )
        self.addCleanup(manager.close)
        login = self.root / "runner/logins/codex/1"
        login.mkdir(parents=True)
        login.joinpath("login.json").write_bytes(canonical(login_files()))
        mediation = ConnectionServer(("127.0.0.1", 0), manager, "c" * 48, "m" * 48)
        origin = self.serve(mediation)
        connections = WorkerConnections(origin, "m" * 48)
        connections.bind(self.runtime)
        self.runtime.connections = connections
        client = CompactClient(
            worker, origin, "c" * 48, directory=self.root / "runner/inputs"
        )
        return client, manager, connections

    def prepare(self, client):
        adapter = NativeBackendRegistry().create("codex", client)
        return adapter.prepare(
            run_id=str(uuid4()),
            operation_id=str(uuid4()),
            source=self.source,
            prompt="Inspect source without model calls.",
            model="fixture-model",
            policy=default_execution_policy(),
            deadline=self.manifest["deadline"],
            credential={
                "credential": {
                    "provider": "codex",
                    "kind": "login",
                    "id": 1,
                    "version": 1,
                }
            },
        )

    def test_actual_adapter_and_both_http_channels_return_selected_refresh(self):
        client, manager, connections = self.stack()
        session = self.prepare(client)
        operation = session.manifest["operation_id"]
        record = self.runtime.record(operation)
        record["task_launch_intent"] = True
        self.runtime.save(record)
        envelope = json.loads(
            (self.runtime.path(operation) / "backend-secret").read_text()
        )
        self.assertEqual(envelope["connection"]["files"], login_files())
        self.assertEqual(envelope["scope"]["worker_boot"], self.runtime.boot)
        with patch(
            "vibearr_executor.compact_connections.address", return_value="172.20.0.2"
        ):
            self.assertEqual(
                connections.network(operation, self.profile), [("172.20.0.2", 3128)]
            )
        refreshed = login_files(access="http-renewed-access-token")
        home = self.root / "operation-home"
        home.mkdir()
        install_login("codex", refreshed, home)
        connections.finish(operation, home, record)
        self.assertEqual(manager._login("codex", 1), refreshed)
        self.assertFalse(connections._path(operation).exists())
        self.assertNotIn(
            "http-renewed-access-token", json.dumps(client.worker.status(operation))
        )

    def test_worker_boot_change_rejects_prepared_http_grant(self):
        client, manager, connections = self.stack()
        session = self.prepare(client)
        operation = session.manifest["operation_id"]
        token = connections._state(operation)["grant"]["token"]
        scope = connections._state(operation)["scope"]
        self.runtime.boot = str(uuid4())
        with self.assertRaises(Conflict):
            MediationClient(client.mediation.origin, "m" * 48).request(
                "/v4/connections/activate",
                {"scope": scope, "token": token, "model": "fixture-model"},
            )
        self.assertTrue((manager.login_root / "execution-lease.json").exists())

    def test_actual_http_cancels_prelaunch_without_losing_selected_login(self):
        client, manager, connections = self.stack()
        operation = self.prepare(client).manifest["operation_id"]
        preparing = threading.Event()

        def blocked_preparation(_operation_id, _profile, _record):
            preparing.set()
            if not self.runtime.cancel.wait(5):
                raise RuntimeError("Preparation did not receive cancellation.")
            raise Rejected("Operation cancelled before task launch.")

        with (
            patch.object(
                self.runtime, "prepare_phase", side_effect=blocked_preparation
            ),
            patch.object(self.runtime, "reset"),
            patch("vibearr_executor.slots.terminate_children"),
            patch("vibearr_executor.slots.subprocess.Popen") as launch,
        ):
            try:
                self.assertEqual(client.start(operation)["state"], "running")
                self.assertTrue(preparing.wait(5))
                self.assertIs(
                    self.runtime.record(operation)["task_launch_intent"], False
                )
                self.assertEqual(client.status(operation)["state"], "running")
                stopped = client.stop(operation)
                self.assertEqual(stopped["state"], "exited")
                self.assertTrue(stopped["descendants_terminated"])
                self.assertTrue(stopped["result"]["canceled"])
                launch.assert_not_called()
            finally:
                self.runtime.cancel.set()
                if self.runtime.thread is not None:
                    self.runtime.thread.join(5)
        self.assertEqual(manager._login("codex", 1), login_files())
        self.assertFalse(connections._path(operation).exists())
        self.assertFalse((manager.login_root / "execution-lease.json").exists())
        self.assertFalse(self.runtime.fenced)

    def test_prepared_cancellation_uses_real_worker_stop_and_returns_original_login(
        self,
    ):
        client, manager, connections = self.stack()
        session = self.prepare(client)
        operation = session.manifest["operation_id"]
        result = client.stop(operation)
        self.assertEqual(result["state"], "exited")
        self.assertTrue(result["descendants_terminated"])
        self.assertEqual(manager._login("codex", 1), login_files())
        self.assertFalse(connections._path(operation).exists())
        self.assertFalse((manager.login_root / "execution-lease.json").exists())
        self.assertFalse(self.runtime.fenced)

    def test_connection_lookup_does_not_wait_on_worker_start_lock(self):
        client, manager, connections = self.stack()
        operation = self.prepare(client).manifest["operation_id"]
        client.worker.timeout = 1
        with patch(
            "vibearr_executor.compact_connections.address", return_value="172.20.0.2"
        ):
            connections.network(operation, self.profile)
        token = connections._state(operation)["grant"]["token"]
        with self.runtime.lock, self.assertRaises(Rejected):
            manager.grant(token, host="chatgpt.com")

    def test_lost_cancel_return_response_keeps_exact_payload_and_worker_fence(self):
        client, manager, connections = self.stack()
        operation = self.prepare(client).manifest["operation_id"]
        request = connections.client.request

        def interrupted(path, data):
            result = request(path, data)
            if path.endswith("/finish"):
                raise Conflict("Lost finish acknowledgment")
            return result

        with patch.object(connections.client, "request", side_effect=interrupted):
            client.stop(operation)
        self.assertTrue(self.runtime.fenced)
        self.assertEqual(
            connections._state(operation)["return"]["files"], login_files()
        )
        connections.finish(
            operation, self.root / "missing-home", self.runtime.record(operation)
        )
        self.assertFalse(connections._path(operation).exists())
        self.assertEqual(manager._login("codex", 1), login_files())


class ScopedProxyHTTPTests(unittest.TestCase):
    setUp = connection_fixtures.ConnectionTests.setUp
    prepare = connection_fixtures.ConnectionTests.prepare
    activate = connection_fixtures.ConnectionTests.activate

    def connect(self, token, *, expected_lookup=False):
        with Proxy(("127.0.0.1", 0), Handler) as server:
            server.hosts, server.denied = {"chatgpt.com"}, set()
            server.authorize = lambda grant, host: self.manager.grant(grant, host=host)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with socket.socket() as connection:
                    connection.settimeout(5)
                    connection.connect(server.server_address)
                    import base64

                    credential = base64.b64encode(("grant:" + token).encode()).decode()
                    request = (
                        "CONNECT chatgpt.com:443 HTTP/1.1\r\nProxy-Authorization: Basic "
                        + credential
                        + "\r\n\r\n"
                    )
                    addresses = [
                        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.4", 443))
                    ]
                    with patch(
                        "vibearr_executor.proxy.socket.getaddrinfo",
                        return_value=addresses,
                    ) as lookup:
                        connection.sendall(request.encode())
                        response = connection.recv(4096)
                        self.assertEqual(lookup.called, expected_lookup)
                        return response
            finally:
                server.shutdown()
                thread.join(5)

    def test_forged_operation_grant_is_rejected_over_connect(self):
        self.activate()
        self.assertIn(b"403 Forbidden", self.connect("forged-operation-grant"))

    def test_private_dns_is_rejected_even_with_active_selected_grant(self):
        self.assertIn(
            b"403 Forbidden", self.connect(self.activate(), expected_lookup=True)
        )

    def test_expired_operation_grant_is_rejected_before_dns(self):
        token = self.activate()
        self.clock = 1100
        self.assertIn(b"403 Forbidden", self.connect(token))

    def test_revoked_operation_grant_is_rejected_before_dns(self):
        token = self.activate()
        self.manager.revoke(self.worker.scope, token)
        self.assertIn(b"403 Forbidden", self.connect(token))

    def test_public_and_private_mixed_dns_cannot_bypass_policy(self):
        addresses = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (value, 443))
            for value in ("8.8.8.8", "192.168.1.2")
        ]
        with (
            patch("vibearr_executor.proxy.socket.getaddrinfo", return_value=addresses),
            self.assertRaises(ValueError),
        ):
            resolve_public("chatgpt.com", set())


class ScopedRelayHTTPTests(unittest.TestCase):
    setUp = connection_fixtures.ConnectionTests.setUp
    prepare = connection_fixtures.ConnectionTests.prepare
    activate = connection_fixtures.ConnectionTests.activate

    def relay(self, token, model="selected-model", *, expected_lookup=False):
        import http.client
        from .ollama_relay import Handler as RelayHandler, Relay

        with Relay(("127.0.0.1", 0), RelayHandler) as server:
            server.select_connection = self.manager.relay
            server.denied = {"172.20.0.2"}
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                connection = http.client.HTTPConnection(
                    "127.0.0.1", server.server_port, timeout=5
                )
                self.addCleanup(connection.close)
                with patch(
                    "vibearr_executor.ollama_relay.resolve_endpoint",
                    side_effect=ValueError("forbidden resolved destination"),
                ) as lookup:
                    connection.request(
                        "POST",
                        "/v1/chat/completions",
                        body=json.dumps({"model": model}),
                        headers={
                            "X-Vibearr-Grant": token,
                            "Content-Type": "application/json",
                        },
                    )
                    response = connection.getresponse()
                    content = response.read()
                    self.assertEqual(lookup.called, expected_lookup)
                    return response.status, content
            finally:
                server.shutdown()
                thread.join(5)

    def select_ollama(self):
        self.worker.provider = "opencode"
        self.worker.credential_id = 3
        self.connection = {
            "provider": "opencode",
            "credential_id": 3,
            "endpoint": "http://192.168.1.20:11434",
        }
        return self.activate()

    def test_inference_rejects_forged_grant_over_http(self):
        self.select_ollama()
        status, content = self.relay("forged-grant")
        self.assertEqual(status, 503)
        self.assertNotIn(b"forged-grant", content)

    def test_inference_rejects_other_model_over_http(self):
        self.assertEqual(self.relay(self.select_ollama(), "other-model")[0], 503)

    def test_inference_rejects_metadata_or_control_dns_over_http(self):
        self.assertEqual(self.relay(self.select_ollama(), expected_lookup=True)[0], 503)

    def test_revoked_subscription_cannot_call_ollama_relay(self):
        token = self.activate()
        self.manager.revoke(self.worker.scope, token)
        self.assertEqual(self.relay(token)[0], 503)

    def relay_stream(self, *, revoke_during_read=False):
        import http.client
        from unittest.mock import Mock
        from urllib.parse import urlsplit
        from .ollama_relay import Handler as RelayHandler, Relay

        token = self.select_ollama()
        with Relay(("127.0.0.1", 0), RelayHandler) as server:
            server.select_connection = self.manager.relay
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            connection = http.client.HTTPConnection(
                "127.0.0.1", server.server_port, timeout=5
            )
            response = Mock(status=200)
            response.getheader.return_value = "application/json"
            response.read1.side_effect = [b'{"fixture":true}', b""]
            if revoke_during_read:

                def revoked_read(_size):
                    self.manager.revoke(self.worker.scope, token)
                    return b"must-not-forward-after-revocation"

                response.read1.side_effect = revoked_read
            upstream = Mock()
            upstream.getresponse.return_value = response
            endpoint = self.connection["endpoint"]
            try:
                with (
                    patch(
                        "vibearr_executor.ollama_relay.resolve_endpoint",
                        return_value=(urlsplit(endpoint), "192.168.1.20", 11434),
                    ),
                    patch(
                        "vibearr_executor.ollama_relay.http.client.HTTPConnection",
                        return_value=upstream,
                    ) as transport,
                ):
                    connection.request(
                        "POST",
                        "/v1/chat/completions",
                        body=json.dumps({"model": "selected-model"}),
                        headers={
                            "X-Vibearr-Grant": token,
                            "Authorization": "Bearer task-injection",
                        },
                    )
                    reply = connection.getresponse()
                    self.assertEqual(reply.status, 200)
                    self.assertEqual(
                        reply.read(), b"" if revoke_during_read else b'{"fixture":true}'
                    )
                transport.assert_called_once_with("192.168.1.20", 11434, timeout=30)
                headers = upstream.request.call_args.kwargs["headers"]
                self.assertEqual(
                    headers,
                    {"Host": "192.168.1.20:11434", "Content-Type": "application/json"},
                )
            finally:
                connection.close()
                server.shutdown()
                thread.join(5)

    def test_relay_preserves_external_endpoint_and_strips_operation_authentication(
        self,
    ):
        self.relay_stream()

    def test_relay_rechecks_grant_after_a_blocking_upstream_read(self):
        self.relay_stream(revoke_during_read=True)
