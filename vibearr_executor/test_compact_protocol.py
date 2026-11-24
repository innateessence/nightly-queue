from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

from .artifacts import pack
from .client import Client, ExecutorRejected
from .compact_profiles import profiles
from .compact_protocol import PREFIX
from .compact_worker import CompactHandler, CompactWorkerRuntime
from .contracts import canonical, digest
from .integrated import IntegratedHandler, IntegratedRuntime
from .server import Server
from .slots import SlotRuntime


class CompactWriteProtocolTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        installed, _ = profiles("sha256:" + "a" * 64, "b" * 64)
        with (
            patch("vibearr_executor.slots.terminate_children"),
            patch.object(SlotRuntime, "reset"),
        ):
            self.legacy = IntegratedRuntime(
                self.root / "legacy",
                installed,
                proxy=SimpleNamespace(),
                relay=SimpleNamespace(),
            )
            self.compact = CompactWorkerRuntime(self.root / "compact", installed)
        self.addCleanup(self.legacy.close)
        self.addCleanup(self.compact.close)
        boundary = patch(
            "vibearr_executor.slot_safety.binding",
            return_value={"fixture": "HTTP protocol regression only"},
        )
        boundary.start()
        self.addCleanup(boundary.stop)
        self.source = pack({"candidate.txt": (0o644, b"source")})
        context = canonical({"adapter": "codex", "model": "fixture"})
        for runtime in (self.legacy, self.compact):
            runtime.upload("source", self.source, digest(self.source))
            runtime.upload("context", context, digest(context))
        profile = installed[0]
        self.manifest = {
            "schema_version": 1,
            "run_id": str(uuid4()),
            "operation_id": str(uuid4()),
            "profile_id": profile.profile_id,
            "profile_digest": profile.digest,
            "source_digest": digest(self.source),
            "context_digest": digest(context),
            "phase": "agent",
            "limits": profile.data()["limits"],
            "network_hosts": list(profile.network_hosts),
            "deadline": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
        }

    @contextmanager
    def endpoint(self, runtime, handler):
        with Server(("127.0.0.1", 0), handler) as server:
            server.runtime, server.token = runtime, "a" * 48
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with patch.dict(
                    "os.environ", {"VIBEARR_RUNTIME_PROTOCOL": "compact-worker-v1"}
                ):
                    client = Client(
                        f"http://127.0.0.1:{server.server_port}", server.token
                    )
                yield client
            finally:
                server.shutdown()
                thread.join(timeout=5)

    def test_prepare_never_persists_on_actual_legacy_http_runtime(self):
        with self.endpoint(self.legacy, IntegratedHandler) as client:
            with self.assertRaises(ExecutorRejected):
                client.prepare(self.manifest)
        self.assertFalse(self.legacy.path(self.manifest["operation_id"]).exists())
        self.assertEqual(list(self.legacy.operations.iterdir()), [])

    def test_start_never_reaches_actual_legacy_runtime(self):
        operation = self.manifest["operation_id"]
        self.legacy.prepare(self.manifest)
        saved = (self.legacy.path(operation) / "record.json").read_bytes()
        with (
            self.endpoint(self.legacy, IntegratedHandler) as client,
            patch.object(self.legacy, "start", wraps=self.legacy.start) as start,
        ):
            with self.assertRaises(ExecutorRejected):
                client.start(operation)
        start.assert_not_called()
        self.assertEqual(
            (self.legacy.path(operation) / "record.json").read_bytes(), saved
        )
        self.assertIsNone(self.legacy.thread)
        self.assertIsNone(self.legacy.execution_lock)

    def test_upload_cannot_add_an_artifact_on_legacy_runtime(self):
        content = b"new compact-only artifact"
        with self.endpoint(self.legacy, IntegratedHandler) as client:
            with self.assertRaises(ExecutorRejected):
                client.upload("context", content)
        self.assertFalse((self.legacy.artifacts / digest(content)).exists())

    def test_secret_cannot_reach_legacy_connection_loading(self):
        operation = self.manifest["operation_id"]
        self.legacy.prepare(self.manifest)
        with (
            self.endpoint(self.legacy, IntegratedHandler) as client,
            patch.object(
                self.legacy, "supply_secret", wraps=self.legacy.supply_secret
            ) as secret,
        ):
            with self.assertRaises(ExecutorRejected):
                client.supply_secret(operation, '{"provider":"codex"}')
        secret.assert_not_called()
        self.assertFalse((self.legacy.path(operation) / "backend-secret").exists())

    def test_direct_dependency_request_cannot_reach_legacy_mutation(self):
        with (
            self.endpoint(self.legacy, IntegratedHandler) as client,
            patch.object(
                self.legacy.dependency_artifacts,
                "prepare",
                wraps=self.legacy.dependency_artifacts.prepare,
            ) as prepare,
        ):
            with self.assertRaises(ExecutorRejected):
                client.request("/v2/dependencies/prepare", {})
        prepare.assert_not_called()

    def pending_dependency(self, runtime):
        operation = self.manifest["operation_id"]
        runtime.prepare(self.manifest)
        runtime.dependency_builds.path(operation).mkdir()
        runtime.dependency_builds.save(
            {"request": {"build_id": operation}, "state": "running"}
        )
        return operation

    def test_dependency_status_cannot_dispatch_a_prepared_legacy_operation(self):
        operation = self.pending_dependency(self.legacy)
        original = self.legacy.record(operation)
        with (
            self.endpoint(self.legacy, IntegratedHandler) as client,
            patch.object(self.legacy, "start", return_value={}) as start,
        ):
            with self.assertRaises(ExecutorRejected):
                client.dependencies(operation, "status")
        start.assert_not_called()
        self.assertEqual(self.legacy.record(operation), original)
        self.assertEqual(
            self.legacy.dependency_builds.read(operation)["state"], "running"
        )
        self.assertIsNone(self.legacy.thread)

    def test_compact_dependency_status_reconciles_only_its_prepared_operation(self):
        operation = self.pending_dependency(self.compact)
        with (
            self.endpoint(self.compact, CompactHandler) as client,
            patch.object(self.compact, "start", return_value={}) as start,
        ):
            self.assertEqual(
                client.dependencies(operation, "status")["state"], "running"
            )
        start.assert_called_once_with(operation)

    def test_recorded_dependency_stop_still_terminates_legacy_operation(self):
        operation = self.pending_dependency(self.legacy)
        with self.endpoint(self.legacy, IntegratedHandler) as client:
            self.assertEqual(
                client.dependencies(operation, "stop")["state"], "canceled"
            )
        result = self.legacy.record(operation)["result"]
        self.assertTrue(result["terminated"])
        self.assertTrue(result["canceled"])

    def test_unversioned_dependency_status_cannot_start_on_compact_worker(self):
        operation = self.pending_dependency(self.compact)
        with (
            self.endpoint(self.compact, CompactHandler) as client,
            patch.object(self.compact, "start", return_value={}) as start,
        ):
            client.expected_runtime = ""
            with self.assertRaises(ExecutorRejected):
                client.dependencies(operation, "status")
        start.assert_not_called()

    def test_compact_prepare_and_terminal_start_replay_use_versioned_route(self):
        operation = self.manifest["operation_id"]
        with self.endpoint(self.compact, CompactHandler) as client:
            prepared = client.prepare(self.manifest)
            self.assertEqual(
                prepared["execution_identity"]["runtime"], "compact-worker-v1"
            )
            self.assertEqual(client.prepare(self.manifest), prepared)
            stopped = client.stop(operation)
            self.assertEqual(client.start(operation), stopped)

    def test_compact_server_rejects_unversioned_new_writes(self):
        with self.endpoint(self.compact, CompactHandler) as client:
            client.expected_runtime = ""
            with self.assertRaises(ExecutorRejected):
                client.prepare(self.manifest)
        self.assertFalse(self.compact.path(self.manifest["operation_id"]).exists())

    def test_versioned_handler_cannot_dispatch_a_replaced_legacy_runtime(self):
        with self.endpoint(self.legacy, CompactHandler) as client:
            with self.assertRaises(ExecutorRejected):
                client.prepare(self.manifest)
        self.assertFalse(self.legacy.path(self.manifest["operation_id"]).exists())

    def test_recorded_legacy_status_and_termination_remain_available(self):
        operation = self.manifest["operation_id"]
        original = self.legacy.prepare(self.manifest)
        with self.endpoint(self.legacy, IntegratedHandler) as client:
            self.assertEqual(client.status(operation), original)
            stopped = client.stop(operation)
            self.assertTrue(stopped["result"]["terminated"])
            self.assertEqual(
                stopped["execution_identity"], original["execution_identity"]
            )
            receipt = client.termination_receipt(operation)
            self.assertEqual(
                receipt["instance_id"], original["execution_identity"]["instance_id"]
            )
            self.assertEqual(
                receipt["manifest_digest"], digest(canonical(self.manifest))
            )
            self.assertTrue(receipt["resources_terminated"])
            with self.assertRaises(ExecutorRejected):
                client.start(operation)

    def test_every_other_post_uses_the_compact_namespace(self):
        operation = str(uuid4())
        paths = [
            "/v1/prepare",
            "/v1/artifacts",
            "/v1/dependency-builds/prepare",
            f"/v1/operations/{operation}/resume",
            f"/v1/operations/{operation}/pause",
            f"/v1/operations/{operation}/purge",
            f"/v1/operations/{operation}/secret",
            f"/v1/dependency-builds/{operation}/start",
            f"/v1/dependency-builds/{operation}/status",
            f"/v1/dependency-builds/{operation}/resume",
            "/v2/dependencies/prepare",
            f"/v2/dependencies/{operation}/chunks",
            "/v3/qualify",
            "/v4/qualify",
            "/v4/qualify-roles",
            "/v4/restart-witness",
        ]
        with self.endpoint(self.legacy, IntegratedHandler) as client:
            for path in paths:
                with (
                    self.subTest(path=path),
                    patch.object(client.opener, "open") as opened,
                ):
                    opened.return_value.__enter__.return_value.read.side_effect = [
                        b"{}",
                        b"",
                    ]
                    client.request(path, {})
                    self.assertEqual(
                        opened.call_args.args[0].full_url, client.url + PREFIX + path
                    )
