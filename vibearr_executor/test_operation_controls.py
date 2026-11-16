import json
import tempfile
import threading
import unittest
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from .artifacts import pack, result_manifest
from .client import Client, ExecutorArtifactRemoved
from .contracts import ArtifactRemoved, Conflict, Rejected, canonical, digest
from .engine import EngineError
from .qualify import smoke_profiles
from .runtime import Runtime, durable
from .server import Handler, Server


class OperationControlTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.profiles = smoke_profiles("sha256:" + "a" * 64)
        self.runtime = Runtime(self.root, self.profiles)
        self.addCleanup(lambda: self.runtime.close())
        self.source = pack({"task.txt": (0o644, b"original\n")})
        self.context = b"fixed context"
        self.runtime.upload("source", self.source, digest(self.source))
        self.runtime.upload("context", self.context, digest(self.context))
        profile = self.profiles[0]
        self.manifest = {
            "schema_version": 1,
            "run_id": str(uuid4()),
            "operation_id": str(uuid4()),
            "profile_id": profile.profile_id,
            "profile_digest": profile.digest,
            "source_digest": digest(self.source),
            "context_digest": digest(self.context),
            "phase": profile.phase,
            "limits": asdict(profile.limits),
            "deadline": (
                datetime.now(timezone.utc) + timedelta(minutes=10)
            ).isoformat(),
            "network_hosts": list(profile.network_hosts),
        }
        self.operation = self.manifest["operation_id"]
        self.runtime.prepare(self.manifest)
        self.engine = {
            "Id": "original-container",
            "State": {"Running": True, "Paused": False},
        }
        self.addCleanup(patch.stopall)

    def running(self):
        record = self.runtime.record(self.operation)
        record.update(
            state="running",
            launch_intent=True,
            gate_opened=True,
            container_id=self.engine["Id"],
            launch_binding_digest=digest(canonical({"host": "test"})),
        )
        self.runtime.save(record)
        patch.object(self.runtime, "owned", return_value=self.engine).start()
        patch.object(self.runtime, "harvest", return_value=False).start()
        patch(
            "vibearr_executor.qualification.binding", return_value={"host": "test"}
        ).start()
        patch(
            "vibearr_executor.qualification.capabilities",
            return_value={"qualified": True},
        ).start()

    def command(self, action, *_args, **_kwargs):
        if action == "pause":
            self.engine["State"]["Paused"] = True
        if action == "unpause":
            self.engine["State"]["Paused"] = False
        return b""

    def cleanup(self, record):
        record.update(
            state="exited",
            descendants_terminated=True,
            paused=False,
            pause_intent=False,
        )
        self.runtime.save(record)

    def terminal(self, operation=None):
        operation = operation or self.operation
        record = self.runtime.record(operation)
        record.update(state="exited", descendants_terminated=True)
        record["result"] = {
            "schema_version": 1,
            "run_id": record["manifest"]["run_id"],
            "operation_id": operation,
            "exit_code": 0,
            "terminated": True,
            "log_digest": digest(b"output"),
            "artifact": result_manifest(self.source, self.source),
        }
        self.runtime.save(record)
        durable(self.runtime.path(operation) / "source.tar", self.source)
        durable(self.runtime.path(operation) / "logs.bin", b"output")
        return record

    def test_pause_and_resume_keep_original_identity_and_deadline(self):
        self.running()
        original = self.runtime.record(self.operation)
        with patch.object(
            self.runtime.docker, "run", side_effect=self.command
        ) as engine:
            self.assertTrue(self.runtime.pause(self.operation)["paused"])
            self.assertTrue(self.runtime.pause(self.operation)["paused"])
            self.assertFalse(self.runtime.resume(self.operation)["paused"])
        self.assertEqual(
            [call.args[0] for call in engine.call_args_list], ["pause", "unpause"]
        )
        current = self.runtime.record(self.operation)
        self.assertEqual(current["container_id"], original["container_id"])
        self.assertEqual(current["manifest"], original["manifest"])
        self.assertEqual(current["deadline_monotonic"], original["deadline_monotonic"])

    def test_lost_pause_response_reconciles_same_container(self):
        self.running()

        def lost(*args, **kwargs):
            self.command(*args, **kwargs)
            raise EngineError("response lost after actual pause")

        with (
            patch.object(self.runtime.docker, "run", side_effect=lost),
            self.assertRaises(EngineError),
        ):
            self.runtime.pause(self.operation)
        self.assertTrue(self.runtime.record(self.operation)["pause_intent"])
        with patch.object(self.runtime.docker, "run") as engine:
            result = self.runtime.pause(self.operation)
        self.assertTrue(result["paused"])
        self.assertEqual(result["container_id"], "original-container")
        engine.assert_not_called()

    def test_lost_resume_response_is_readback_only_on_retry(self):
        self.running()
        with patch.object(self.runtime.docker, "run", side_effect=self.command):
            self.runtime.pause(self.operation)

        def lost(*args, **kwargs):
            self.command(*args, **kwargs)
            raise EngineError("response lost after actual unpause")

        with (
            patch.object(self.runtime.docker, "run", side_effect=lost),
            self.assertRaises(EngineError),
        ):
            self.runtime.resume(self.operation)
        with patch.object(self.runtime.docker, "run") as engine:
            self.assertFalse(self.runtime.resume(self.operation)["paused"])
        engine.assert_not_called()

    def test_paused_status_never_executes_a_readback_process(self):
        self.running()
        self.engine["State"]["Paused"] = True
        with patch.object(self.runtime, "harvest") as harvest:
            self.assertTrue(self.runtime.status(self.operation)["paused"])
        harvest.assert_not_called()

    def test_expired_paused_operation_is_forced_terminal_without_unpause(self):
        self.running()
        self.engine["State"]["Paused"] = True
        record = self.runtime.record(self.operation)
        record["deadline_monotonic"] = 0
        self.runtime.save(record)
        with (
            patch.object(self.runtime, "cleanup", side_effect=self.cleanup),
            patch.object(self.runtime.docker, "run") as engine,
        ):
            result = self.runtime.resume(self.operation)
        self.assertEqual(result["state"], "exited")
        self.assertTrue(result["descendants_terminated"])
        self.assertTrue(result["result"]["timed_out"])
        self.assertNotIn("artifact", result["result"])
        engine.assert_not_called()

    def test_cancel_paused_operation_does_not_wait_for_frozen_init(self):
        self.running()
        self.engine["State"]["Paused"] = True
        with (
            patch.object(self.runtime, "cleanup", side_effect=self.cleanup),
            patch.object(self.runtime, "harvest") as harvest,
        ):
            result = self.runtime.stop(self.operation)
        self.assertTrue(result["result"]["canceled"])
        self.assertEqual(result["state"], "exited")
        harvest.assert_not_called()

    def test_changed_binding_cannot_resume_but_can_still_stop(self):
        self.running()
        self.engine["State"]["Paused"] = True
        with (
            patch(
                "vibearr_executor.qualification.binding",
                return_value={"host": "changed"},
            ),
            self.assertRaises(Rejected),
        ):
            self.runtime.resume(self.operation)
        with patch.object(self.runtime, "cleanup", side_effect=self.cleanup):
            self.assertEqual(self.runtime.stop(self.operation)["state"], "exited")

    def test_unavailable_engine_retains_pause_intent_and_execution_fence(self):
        self.running()
        with (
            patch.object(
                self.runtime.docker, "run", side_effect=EngineError("offline")
            ),
            self.assertRaises(EngineError),
        ):
            self.runtime.pause(self.operation)
        record = self.runtime.record(self.operation)
        self.assertTrue(record["pause_intent"])
        self.assertTrue(record["launch_intent"])
        self.assertEqual(record["state"], "running")

    def test_unknown_execution_cannot_resume(self):
        record = self.runtime.record(self.operation)
        record["state"] = "unknown"
        self.runtime.save(record)
        with self.assertRaises(Conflict):
            self.runtime.resume(self.operation)

    def test_never_started_expiry_erases_secret_without_engine_or_spawn(self):
        self.runtime.supply_secret(self.operation, "private-selected-key-1234")
        record = self.runtime.record(self.operation)
        record["deadline_monotonic"] = 0
        self.runtime.save(record)
        self.runtime.close()
        self.runtime = Runtime(self.root, self.profiles)
        with patch.object(self.runtime.docker, "run") as engine:
            result = self.runtime.status(self.operation)
            replay = self.runtime.start(self.operation)
        self.assertEqual(result["result"]["termination"], "expired-before-start")
        self.assertEqual(replay, result)
        self.assertEqual(
            (self.runtime.path(self.operation) / "input/secret").read_bytes(), b""
        )
        engine.assert_not_called()

    def test_expired_operation_rejects_late_secret(self):
        record = self.runtime.record(self.operation)
        record["deadline_monotonic"] = 0
        self.runtime.save(record)
        with self.assertRaises(Rejected):
            self.runtime.supply_secret(self.operation, "late-private-selected-key")
        self.assertEqual(
            (self.runtime.path(self.operation) / "input/secret").read_bytes(), b""
        )

    def test_purge_rejects_wrong_run_before_deletion(self):
        self.terminal()
        with self.assertRaises(Conflict):
            self.runtime.purge(self.operation, str(uuid4()), ["source", "logs"])
        self.assertTrue((self.runtime.path(self.operation) / "source.tar").exists())

    def test_purge_requires_confirmed_terminal_state(self):
        for state, terminated in (
            ("running", False),
            ("unknown", True),
            ("exited", False),
        ):
            with self.subTest(state=state, terminated=terminated):
                record = self.runtime.record(self.operation)
                record.update(state=state, descendants_terminated=terminated)
                self.runtime.save(record)
                with self.assertRaises(Conflict):
                    self.runtime.purge(
                        self.operation, self.manifest["run_id"], ["logs"]
                    )

    def test_purge_resource_lists_are_strict(self):
        self.terminal()
        for value in ([], ["source", "source"], ["other"], "source", [True]):
            with self.subTest(value=value), self.assertRaises(Rejected):
                self.runtime.purge(self.operation, self.manifest["run_id"], value)

    def test_source_purge_preserves_logs_and_terminal_identity(self):
        original = self.terminal()
        result = self.runtime.purge(self.operation, self.manifest["run_id"], ["source"])
        self.assertEqual(self.runtime.logs(self.operation, 0, 65536), b"output")
        self.assertFalse(result["tombstone"])
        self.assertEqual(
            self.runtime.record(self.operation)["manifest"], original["manifest"]
        )
        self.assertEqual(
            result["receipt_digest"], digest(canonical(original["result"]))
        )
        with self.assertRaises(ArtifactRemoved):
            self.runtime.export(self.operation)

    def test_logs_purge_preserves_verified_source(self):
        self.terminal()
        self.runtime.purge(self.operation, self.manifest["run_id"], ["logs"])
        self.assertEqual(self.runtime.export(self.operation)[1], self.source)
        with self.assertRaises(ArtifactRemoved):
            self.runtime.logs(self.operation, 0, 65536)

    def test_shared_artifacts_survive_until_last_source_reference_is_purged(self):
        other = {**self.manifest, "operation_id": str(uuid4()), "run_id": str(uuid4())}
        self.runtime.prepare(other)
        self.terminal()
        self.runtime.purge(self.operation, self.manifest["run_id"], ["source", "logs"])
        self.assertTrue((self.runtime.artifacts / digest(self.source)).exists())
        self.runtime.stop(other["operation_id"])
        self.runtime.purge(other["operation_id"], other["run_id"], ["source", "logs"])
        self.assertFalse((self.runtime.artifacts / digest(self.source)).exists())

    def test_tombstone_replay_after_restart_cannot_launch(self):
        self.terminal()
        result = self.runtime.purge(
            self.operation, self.manifest["run_id"], ["source", "logs"]
        )
        self.runtime.close()
        self.runtime = Runtime(self.root, self.profiles)
        with patch.object(self.runtime.docker, "run") as engine:
            self.assertEqual(self.runtime.prepare(self.manifest), result)
            self.assertEqual(self.runtime.start(self.operation), result)
            self.assertEqual(
                self.runtime.purge(
                    self.operation, self.manifest["run_id"], ["logs", "source"]
                ),
                result,
            )
        engine.assert_not_called()

    def test_full_purge_restores_operation_admission_capacity(self):
        original = self.terminal()
        for _ in range(999):
            operation = str(uuid4())
            record = json.loads(json.dumps(original))
            record["operation_id"] = operation
            record["manifest"]["operation_id"] = operation
            record["result"]["operation_id"] = operation
            self.runtime.path(operation).mkdir()
            self.runtime.save(record)
        candidate = {**self.manifest, "operation_id": str(uuid4())}
        with self.assertRaises(Rejected):
            self.runtime.prepare(candidate)
        self.runtime.purge(self.operation, self.manifest["run_id"], ["source"])
        with self.assertRaises(Rejected):
            self.runtime.prepare(candidate)
        self.runtime.purge(self.operation, self.manifest["run_id"], ["logs"])
        self.assertEqual(self.runtime.prepare(candidate)["state"], "not-started")

    def test_http_removed_artifact_has_distinct_sdk_error(self):
        self.terminal()
        self.runtime.purge(self.operation, self.manifest["run_id"], ["logs"])
        with Server(("127.0.0.1", 0), Handler) as server:
            server.runtime, server.token = self.runtime, "x" * 64
            thread = threading.Thread(target=server.serve_forever)
            thread.start()
            try:
                client = Client(f"http://127.0.0.1:{server.server_port}", server.token)
                with self.assertRaises(ExecutorArtifactRemoved) as caught:
                    client.logs(self.operation)
                self.assertEqual(caught.exception.http_status, 410)
            finally:
                server.shutdown()
                thread.join()

    def test_missing_stop_has_distinct_http_404_without_creating_an_identity(self):
        from .client import ExecutorNotFound

        missing = str(uuid4())
        with Server(("127.0.0.1", 0), Handler) as server:
            server.runtime, server.token = self.runtime, "x" * 64
            thread = threading.Thread(target=server.serve_forever)
            thread.start()
            try:
                client = Client(f"http://127.0.0.1:{server.server_port}", server.token)
                for call in (
                    lambda: client.stop(missing),
                    lambda: client.dependencies(missing, "stop"),
                    lambda: client.termination_receipt(missing),
                ):
                    with self.assertRaises(ExecutorNotFound) as caught:
                        call()
                    self.assertEqual(caught.exception.http_status, 404)
                self.assertFalse(self.runtime.path(missing).exists())
            finally:
                server.shutdown()
                thread.join()

    def test_http_prepared_presence_binds_manifest_without_launching(self):
        with Server(("127.0.0.1", 0), Handler) as server:
            server.runtime, server.token = self.runtime, "x" * 64
            thread = threading.Thread(target=server.serve_forever)
            thread.start()
            try:
                client = Client(f"http://127.0.0.1:{server.server_port}", server.token)
                with patch.object(self.runtime.docker, "run") as engine:
                    receipt = client.termination_receipt(self.operation)
                engine.assert_not_called()
                self.assertEqual(receipt["operation_id"], self.operation)
                self.assertEqual(receipt["run_id"], self.manifest["run_id"])
                self.assertEqual(
                    receipt["manifest_digest"], digest(canonical(self.manifest))
                )
                self.assertEqual(receipt["instance_id"], self.runtime.instance)
                self.assertFalse(receipt["resources_terminated"])
                self.assertEqual(receipt["state"], "not-started")
            finally:
                server.shutdown()
                thread.join()

    def test_http_terminal_presence_retains_original_receipt_after_purge(self):
        original = self.terminal()
        self.runtime.purge(self.operation, self.manifest["run_id"], ["source", "logs"])
        with Server(("127.0.0.1", 0), Handler) as server:
            server.runtime, server.token = self.runtime, "x" * 64
            thread = threading.Thread(target=server.serve_forever)
            thread.start()
            try:
                client = Client(f"http://127.0.0.1:{server.server_port}", server.token)
                receipt = client.termination_receipt(self.operation)
                self.assertTrue(receipt["resources_terminated"])
                self.assertEqual(
                    receipt["receipt_digest"], digest(canonical(original["result"]))
                )
                self.assertEqual(
                    receipt["manifest_digest"], digest(canonical(self.manifest))
                )
            finally:
                server.shutdown()
                thread.join()

    def test_http_presence_requires_authentication_and_strict_uuid(self):
        from .client import ExecutorRejected

        with Server(("127.0.0.1", 0), Handler) as server:
            server.runtime, server.token = self.runtime, "x" * 64
            thread = threading.Thread(target=server.serve_forever)
            thread.start()
            try:
                client = Client(f"http://127.0.0.1:{server.server_port}", "y" * 64)
                with self.assertRaises(ExecutorRejected) as caught:
                    client.termination_receipt(self.operation)
                self.assertEqual(caught.exception.http_status, 401)
                with self.assertRaises(ValueError):
                    client.termination_receipt("../other")
            finally:
                server.shutdown()
                thread.join()
