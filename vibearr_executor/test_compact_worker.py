import base64
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import signal
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from .artifacts import pack
from .client import Client, ExecutorUnavailable
from .compact_profiles import REQUIRED_BOUNDARY_CHECKS, RUNTIME, profiles
from .compact_worker import CompactHandler, CompactWorkerRuntime
from .contracts import Conflict, PhaseEvidenceRejected, Rejected, canonical, digest
from .runtime import durable
from .slot_http import SlotServer
from .slots import SlotRuntime
from vibearr_runner.native_cli import NativeCLIAdapter
from vibearr_runner.native_sessions import NativeSessionError
from vibearr_runner.policy import ExecutionPolicy, default_execution_policy


class CompactWorkerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        installed, validation = profiles("sha256:" + "a" * 64, "b" * 64)
        with (
            patch("vibearr_executor.slots.terminate_children"),
            patch.object(SlotRuntime, "reset"),
        ):
            self.runtime = CompactWorkerRuntime(
                self.root, installed, validation_profile=validation.data()
            )
        self.addCleanup(self.runtime.close)
        boundary = patch.object(
            self.runtime,
            "boundary",
            return_value={"runtime": RUNTIME, "fixture": "metadata-test-only"},
        )
        boundary.start()
        self.addCleanup(boundary.stop)
        self.profile = installed[0]
        self.source = pack({"candidate.txt": (0o644, b"source")})
        self.context = canonical({"adapter": "codex", "model": "fixture"})
        for kind, content in (("source", self.source), ("context", self.context)):
            self.runtime.upload(kind, content, digest(content))
        self.manifest = {
            "schema_version": 1,
            "run_id": str(uuid4()),
            "operation_id": str(uuid4()),
            "profile_id": self.profile.profile_id,
            "profile_digest": self.profile.digest,
            "source_digest": digest(self.source),
            "context_digest": digest(self.context),
            "phase": "agent",
            "limits": self.profile.data()["limits"],
            "deadline": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
            "network_hosts": list(self.profile.network_hosts),
        }

    def report(self):
        value = {
            "schema_version": 1,
            "runtime": RUNTIME,
            "worker_boot": self.runtime.boot,
            "binding_digest": self.runtime.execution_identity()["binding_digest"],
            "passed": True,
            "checks": sorted(REQUIRED_BOUNDARY_CHECKS),
            "restart_receipt": "metadata-fixture",
            "receipts": ["metadata-fixture"],
        }
        durable(self.root / "compact-qualification.json", canonical(value))
        return value

    def test_compact_identity_uses_one_actual_worker(self):
        prepared = self.runtime.prepare(self.manifest)
        self.assertEqual(prepared["execution_identity"]["runtime"], RUNTIME)
        self.assertEqual(
            prepared["execution_identity"]["instance_id"], self.runtime.instance
        )
        self.assertNotIn("workers", self.runtime.capabilities())

    def test_missing_qualification_cannot_accept_new_work(self):
        capabilities = self.runtime.capabilities()
        self.assertFalse(capabilities["qualified"])
        self.assertFalse(capabilities["accepting_work"])
        self.assertEqual(capabilities["factory_roles"], {})

    def test_boot_change_invalidates_compact_qualification(self):
        self.report()
        self.assertTrue(self.runtime.capabilities()["qualified"])
        self.runtime.boot = str(uuid4())
        self.assertFalse(self.runtime.capabilities()["qualified"])

    def test_incomplete_boundary_checks_never_qualify(self):
        report = self.report()
        report["checks"].remove("production-network-denial")
        durable(self.root / "compact-qualification.json", canonical(report))
        self.assertFalse(self.runtime.capabilities()["qualified"])

    def test_capabilities_publish_actual_validation_profile(self):
        capability = self.runtime.capabilities()
        self.assertEqual(capability["validation_profile"]["image"], self.profile.image)
        self.assertEqual(
            json.loads((self.root / "validation-profile.json").read_bytes()),
            capability["validation_profile"],
        )

    def test_connection_scope_cannot_move_to_a_new_boot(self):
        self.runtime.prepare(self.manifest)
        value = self.runtime.connection_binding(self.manifest["operation_id"])
        self.assertEqual(value["scope"]["source_digest"], digest(self.source))
        self.assertEqual(value["provider"], "codex")
        self.assertEqual(value["state"], "not-started")
        self.assertFalse(value["descendants_terminated"])
        self.runtime.boot = str(uuid4())
        with self.assertRaises(Conflict):
            self.runtime.connection_binding(self.manifest["operation_id"])

    def test_worker_does_not_load_persistent_account_files(self):
        self.runtime.prepare(self.manifest)
        with self.assertRaisesRegex(Rejected, "mediation"):
            self.runtime.supply_secret(
                self.manifest["operation_id"], '{"provider":"codex"}'
            )

    def test_conflicting_duplicate_prepare_preserves_original_operation(self):
        original = self.runtime.prepare(self.manifest)
        altered = {**self.manifest, "run_id": str(uuid4())}
        with self.assertRaises(Conflict):
            self.runtime.prepare(altered)
        self.assertEqual(self.runtime.status(self.manifest["operation_id"]), original)

    def test_phase_lock_refuses_a_second_prepared_operation(self):
        self.runtime.prepare(self.manifest)
        self.runtime.active = str(uuid4())
        with self.assertRaisesRegex(Conflict, "phase"):
            self.runtime.start(self.manifest["operation_id"])
        self.runtime.active = ""

    def test_unknown_prepare_cleanup_fences_reuse(self):
        self.runtime.prepare(self.manifest)
        durable(
            self.runtime.path(self.manifest["operation_id"]) / "backend-secret",
            b"fixture",
        )
        with (
            patch("vibearr_executor.slots.terminate_children"),
            patch.object(
                self.runtime, "capabilities", return_value={"qualified": True}
            ),
            patch.object(
                self.runtime,
                "prepare_phase",
                side_effect=RuntimeError("failed prepare"),
            ),
            patch.object(
                self.runtime, "reset", side_effect=RuntimeError("unknown cleanup")
            ),
        ):
            self.runtime.start(self.manifest["operation_id"])
            self.runtime.thread.join(timeout=2)
        self.assertIn("unknown", self.runtime.fenced)

    def test_browser_output_cannot_be_exported_for_an_agent_operation(self):
        self.runtime.prepare(self.manifest)
        with self.assertRaises(Conflict):
            self.runtime.browser_result(self.manifest["operation_id"])

    def test_factory_context_cannot_change_registered_role(self):
        profile = self.runtime.profiles["factory-intake"]
        context = canonical({"factory_role": "build"})
        self.runtime.upload("context", context, digest(context))
        altered = {
            **self.manifest,
            "profile_id": profile.profile_id,
            "profile_digest": profile.digest,
            "context_digest": digest(context),
        }
        with self.assertRaisesRegex(Rejected, "role"):
            self.runtime.prepare(altered)

    def factory_manifest(self, context):
        profile = self.runtime.profiles["factory-intake"]
        content = canonical(context)
        self.runtime.upload("context", content, digest(content))
        return {
            **self.manifest,
            "profile_id": profile.profile_id,
            "profile_digest": profile.digest,
            "context_digest": digest(content),
        }

    def test_factory_role_cannot_be_omitted_from_a_normal_context(self):
        with self.assertRaisesRegex(Rejected, "role"):
            self.runtime.prepare(self.factory_manifest({"adapter": "codex"}))

    def test_duplicate_factory_prepare_replays_the_original_binding(self):
        manifest = self.factory_manifest({"factory_role": "intake"})
        original = self.runtime.prepare(manifest)
        self.assertEqual(self.runtime.prepare(manifest), original)

    def test_factory_artifact_path_is_validated_before_access(self):
        manifest = self.factory_manifest({"factory_role": "intake"})
        manifest["context_digest"] = "../../private-token"
        with self.assertRaises(Rejected):
            self.runtime.prepare(manifest)

    def test_browser_result_rejects_a_nonzero_phase_even_with_output(self):
        self.runtime.prepare(self.manifest)
        record = self.runtime.record(self.manifest["operation_id"])
        record["manifest"]["profile_id"] = "browser-ui"
        record.update(
            state="exited",
            descendants_terminated=True,
            result={
                "exit_code": 1,
                "terminated": True,
                "timed_out": False,
                "canceled": False,
            },
        )
        self.runtime.save(record)
        with self.assertRaisesRegex(Conflict, "successful"):
            self.runtime.browser_result(self.manifest["operation_id"])

    def test_prepared_cancel_returns_credentials_after_revocation(self):
        operation = self.manifest["operation_id"]
        self.runtime.prepare(self.manifest)
        self.runtime.connections = Mock()
        result = self.runtime.stop(operation)
        self.assertTrue(result["result"]["canceled"])
        self.runtime.connections.revoke.assert_called_once_with(operation)
        self.runtime.connections.finish.assert_called_once_with(
            operation, self.runtime.home, self.runtime.record(operation)
        )

    def test_unknown_revocation_still_stops_and_fences(self):
        operation = self.manifest["operation_id"]
        self.runtime.prepare(self.manifest)
        self.runtime.connections = Mock()
        self.runtime.connections.revoke.side_effect = OSError("offline")
        result = self.runtime.stop(operation)
        self.assertTrue(result["descendants_terminated"])
        self.assertTrue(self.runtime.fenced)

    def test_browser_evidence_rejection_does_not_skip_database_cleanup(self):
        self.assert_cleanup_after_phase_error(
            PhaseEvidenceRejected("missing capture"), True
        )

    def test_unknown_service_stop_still_resets_database_but_keeps_fence(self):
        self.assert_cleanup_after_phase_error(RuntimeError("unknown app stop"), False)

    def assert_cleanup_after_phase_error(self, error, terminated):
        operation = self.manifest["operation_id"]
        self.runtime.prepare(self.manifest)
        record = self.runtime.record(operation)
        record["database_url"] = "postgresql://task:private-password@localhost/task"
        self.runtime.save(record)
        exported = self.runtime.path(operation) / "source.tar"
        exported.write_bytes(b"candidate export")
        self.runtime.database = Mock()
        self.runtime.cancel.set()
        with (
            patch("vibearr_executor.slots.terminate_children"),
            patch.object(self.runtime, "end_phase", side_effect=error),
            patch.object(self.runtime, "reset") as reset,
        ):
            self.runtime.run(operation, self.profile, {}, b"")
        self.runtime.database.reset.assert_called_once_with(operation)
        reset.assert_called_once_with()
        result = self.runtime.record(operation)["result"]
        self.assertEqual(result["terminated"], terminated)
        self.assertTrue(result["export_error"])
        self.assertEqual(bool(self.runtime.fenced), not terminated)
        if terminated:
            self.assertFalse(exported.exists())

    def test_pause_stops_both_task_uids_before_suspending_egress(self):
        operation = self.manifest["operation_id"]
        self.runtime.prepare(self.manifest)
        record = self.runtime.record(operation)
        record["state"] = "running"
        self.runtime.save(record)
        self.runtime.active = operation
        self.runtime.process = SimpleNamespace(pid=123)
        self.runtime.connections = Mock()
        with (
            patch("os.killpg"),
            patch("vibearr_executor.slot_processes.signal_tasks") as tasks,
        ):
            self.runtime.pause(operation)
        tasks.assert_called_once_with(signal.SIGSTOP, (1000, 10003))
        self.runtime.connections.suspend.assert_called_once_with(operation)
        self.runtime.process = None
        self.runtime.active = ""


class CompactClientTests(unittest.TestCase):
    def test_private_registered_worker_origin_is_allowed(self):
        with (
            patch.dict("os.environ", {"VIBEARR_RUNTIME_PROTOCOL": RUNTIME}),
            patch(
                "socket.getaddrinfo", return_value=[(2, 1, 6, "", ("172.20.0.4", 8791))]
            ),
        ):
            client = Client("http://worker:8791", "a" * 48)
        self.assertEqual(client.expected_runtime, RUNTIME)

    def test_an_arbitrary_private_executor_is_not_registered(self):
        with patch.dict("os.environ", {"VIBEARR_RUNTIME_PROTOCOL": RUNTIME}):
            with self.assertRaises(Rejected):
                Client("http://another-service:8791", "a" * 48)

    def test_https_does_not_authorize_an_unregistered_compact_origin(self):
        with patch.dict("os.environ", {"VIBEARR_RUNTIME_PROTOCOL": RUNTIME}):
            with self.assertRaisesRegex(Rejected, "registered"):
                Client("https://arbitrary.example", "a" * 48)

    def test_public_resolution_of_registered_worker_is_rejected(self):
        with (
            patch.dict("os.environ", {"VIBEARR_RUNTIME_PROTOCOL": RUNTIME}),
            patch(
                "socket.getaddrinfo", return_value=[(2, 1, 6, "", ("8.8.8.8", 8791))]
            ),
        ):
            with self.assertRaises(Rejected):
                Client("http://worker:8791", "a" * 48)

    def test_mixed_boundary_cannot_be_used_for_a_new_prepare(self):
        with patch.dict("os.environ", {"VIBEARR_RUNTIME_PROTOCOL": RUNTIME}):
            client = Client("http://127.0.0.1:8791", "a" * 48)
        with patch.object(
            client,
            "request",
            return_value={"execution_identity": {"runtime": "integrated-runner-v1"}},
        ):
            with self.assertRaises(ExecutorUnavailable):
                client.prepare({})

    def test_browser_transport_rejects_tampered_bytes(self):
        client = Client("http://127.0.0.1:8791", "a" * 48)
        response = {
            "content": base64.b64encode(b"png archive").decode(),
            "bytes": 11,
            "sha256": "a" * 64,
        }
        with patch.object(client, "request", return_value=response):
            with self.assertRaises(ExecutorUnavailable):
                client.browser_result(str(uuid4()))


class CompactHTTPTests(unittest.TestCase):
    setUp = CompactWorkerTests.setUp

    def test_authenticated_direct_prepare_and_replay_use_the_real_handler(self):
        with SlotServer(("127.0.0.1", 0), CompactHandler) as server:
            server.runtime, server.token = self.runtime, "a" * 48
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with patch.dict("os.environ", {"VIBEARR_RUNTIME_PROTOCOL": RUNTIME}):
                    client = Client(
                        f"http://127.0.0.1:{server.server_port}", server.token
                    )
                observed = client.prepare(self.manifest)
                self.assertEqual(client.prepare(self.manifest), observed)
                self.assertEqual(client.status(self.manifest["operation_id"]), observed)
                self.assertEqual(client.capabilities()["runtime"], RUNTIME)
            finally:
                server.shutdown()
                thread.join(timeout=5)


class CompactAdapterTests(unittest.TestCase):
    def setUp(self):
        installed, _ = profiles("sha256:" + "a" * 64, "b" * 64)
        self.profile = installed[0]
        self.identity = {
            "runtime": RUNTIME,
            "protocol_version": 2,
            "instance_id": str(uuid4()),
            "worker_boot": str(uuid4()),
            "binding_digest": "b" * 64,
        }
        self.capabilities = {
            **self.identity,
            "schema_version": 1,
            "qualification_version": 1,
            "qualified": True,
            "profiles": [
                {**self.profile.data(), "profile_digest": self.profile.digest}
            ],
        }

    def test_actual_compact_profiles_pass_validation_diagnostics(self):
        from vibearr_runner.native_validation import validation_diagnostics

        installed, validation = profiles(self.profile.image, "b" * 64)
        capabilities = {
            **self.capabilities,
            "schema_version": 1,
            "protocol_version": 2,
            "qualification_version": 1,
            "accepting_work": True,
            "profiles": [
                {**profile.data(), "profile_digest": profile.digest}
                for profile in installed
            ],
        }
        result = validation_diagnostics(
            default_execution_policy(), profile=validation, capabilities=capabilities
        )
        self.assertTrue(result["ready"], result)
        capabilities["qualification_version"] = 0
        result = validation_diagnostics(
            default_execution_policy(), profile=validation, capabilities=capabilities
        )
        self.assertFalse(result["ready"])

    def test_actual_native_adapter_prepares_a_compact_codex_session(self):
        capabilities = self.capabilities
        identity = self.identity
        uploads = {}

        class RecordedClient:
            def capabilities(self):
                return deepcopy(capabilities)

            def request(self, path):
                return {"available": True, "login_digest": "d" * 64}

            def upload(self, kind, content):
                uploads[kind] = content
                return {"sha256": digest(content)}

            def prepare(self, manifest):
                return {"execution_identity": identity}

            def supply_secret(self, operation_id, secret):
                self.secret = secret

        adapter = NativeCLIAdapter(RecordedClient(), "codex")
        session = adapter.prepare(
            run_id=str(uuid4()),
            operation_id=str(uuid4()),
            source=pack({"answer.py": (0o644, b"answer=1\n")}),
            prompt="Inspect the candidate.",
            model="fixture-model",
            policy=default_execution_policy(),
            deadline=(datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
            credential={
                "credential": {
                    "provider": "codex",
                    "kind": "login",
                    "id": 1,
                    "version": 1,
                }
            },
        )
        self.assertEqual(session.execution_identity, identity)
        self.assertEqual(session.manifest["profile_digest"], self.profile.digest)
        self.assertEqual(json.loads(uploads["context"])["adapter"], "codex")

    def test_truthy_qualification_text_cannot_authorize_native_dispatch(self):
        client = SimpleNamespace(
            capabilities=lambda: {**self.capabilities, "qualified": "false"}
        )
        with self.assertRaisesRegex(NativeSessionError, "Qualify"):
            NativeCLIAdapter(client, "codex").selected_profile(
                ExecutionPolicy(), "login"
            )

    def test_incorrect_registered_profile_digest_cannot_authorize_dispatch(self):
        value = deepcopy(self.capabilities)
        value["profiles"][0]["profile_digest"] = "c" * 64
        client = SimpleNamespace(capabilities=lambda: value)
        with self.assertRaisesRegex(NativeSessionError, "digest"):
            NativeCLIAdapter(client, "codex").selected_profile(
                ExecutionPolicy(), "login"
            )
