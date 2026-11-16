import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4
from unittest.mock import patch

from .artifacts import pack
from .client import Client, ExecutorUnavailable
from .contracts import Conflict, Rejected, digest
from .qualify import smoke_profiles
from .runtime import Runtime
from .runtime import ResultInvalid
from .engine import EngineError
from .server import Handler, Server


class RuntimeContractTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        self.profiles = smoke_profiles("sha256:" + "a" * 64)
        self.runtime = Runtime(self.directory, self.profiles)
        self.source, self.context = pack({"a": (0o644, b"a")}), b"context"
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

    def tearDown(self):
        self.runtime.close()
        self.temporary.cleanup()

    def test_prepare_retry_is_stable_after_restart(self):
        first = self.runtime.prepare(self.manifest)
        self.runtime.close()
        self.runtime = Runtime(self.directory, self.profiles)
        self.assertEqual(self.runtime.prepare(self.manifest), first)

    def test_changed_manifest_conflicts(self):
        self.runtime.prepare(self.manifest)
        with self.assertRaises(Conflict):
            self.runtime.prepare({**self.manifest, "context_digest": "c" * 64})

    def test_upload_rejects_digest_mismatch(self):
        with self.assertRaises(Rejected):
            self.runtime.upload("context", b"different", digest(self.context))

    def test_unknown_run_holds_fence_before_another_run_can_launch(self):
        self.runtime.prepare(self.manifest)
        record = self.runtime.record(self.manifest["operation_id"])
        record.update(state="unknown", launch_intent=True)
        self.runtime.save(record)
        other = {**self.manifest, "run_id": str(uuid4()), "operation_id": str(uuid4())}
        self.runtime.prepare(other)
        with (
            patch.object(self.runtime.docker, "run") as engine,
            self.assertRaises(Conflict),
        ):
            self.runtime.start(other["operation_id"])
        engine.assert_not_called()

    def test_changed_smoke_recipe_cannot_bypass_qualification(self):
        from .contracts import Profile

        changed = Profile.parse(
            {**self.profiles[0].data(), "argv": ["/different-command"]}
        )
        self.runtime.profiles[changed.profile_id] = changed
        manifest = {**self.manifest, "profile_digest": changed.digest}
        self.runtime.prepare(manifest)
        with (
            patch(
                "vibearr_executor.qualification.capabilities",
                return_value={"qualified": False},
            ),
            patch.object(self.runtime.docker, "run") as engine,
            self.assertRaises(Rejected),
        ):
            self.runtime.start(manifest["operation_id"])
        engine.assert_not_called()

    def test_stopped_prepared_operation_cannot_later_start(self):
        operation = self.manifest["operation_id"]
        self.runtime.prepare(self.manifest)
        with (
            patch.object(self.runtime, "owned", return_value=None),
            patch.object(self.runtime.docker, "remove"),
        ):
            stopped = self.runtime.stop(operation)
        with patch.object(self.runtime.docker, "run") as engine:
            self.assertEqual(self.runtime.start(operation), stopped)
        engine.assert_not_called()

    def running_record(self):
        self.runtime.prepare(self.manifest)
        record = self.runtime.record(self.manifest["operation_id"])
        record.update(
            state="running", launch_intent=True, gate_intent=True, gate_opened=True
        )
        self.runtime.save(record)
        return record

    def test_result_readback_starvation_preserves_running_identity_and_fence(self):
        record = self.running_record()
        with (
            patch.object(
                self.runtime, "owned", return_value={"State": {"Running": True}}
            ),
            patch.object(
                self.runtime,
                "harvest",
                side_effect=EngineError("PID limit prevents exec"),
            ),
        ):
            result = self.runtime.status(record["operation_id"])
        self.assertEqual(result["state"], "running")
        self.assertIn("temporarily unavailable", result["wait_reason"])
        self.assertTrue(self.runtime.record(record["operation_id"])["launch_intent"])

    def test_result_integrity_failure_becomes_unknown(self):
        record = self.running_record()
        with (
            patch.object(
                self.runtime, "owned", return_value={"State": {"Running": True}}
            ),
            patch.object(
                self.runtime, "harvest", side_effect=ResultInvalid("Digest mismatch")
            ),
        ):
            result = self.runtime.status(record["operation_id"])
        self.assertEqual(result["state"], "unknown")

    def test_start_retry_does_not_exec_again_after_launch_gate_is_confirmed(self):
        record = self.running_record()
        expected = {"operation_id": record["operation_id"], "state": "running"}
        with (
            patch.object(
                self.runtime, "owned", return_value={"State": {"Running": True}}
            ),
            patch.object(self.runtime, "status", return_value=expected),
            patch.object(self.runtime.docker, "run") as engine,
        ):
            self.assertEqual(self.runtime.start(record["operation_id"]), expected)
        engine.assert_not_called()

    def test_only_one_executor_can_own_a_journal(self):
        with self.assertRaises(BlockingIOError):
            Runtime(self.directory, self.profiles)

    def test_secret_is_private_and_outside_manifest(self):
        self.runtime.prepare(self.manifest)
        secret = "selected-private-secret-1234"
        operation = self.manifest["operation_id"]
        self.runtime.supply_secret(operation, secret)
        directory = self.directory / "operations" / operation
        self.assertEqual((directory / "input" / "secret").stat().st_mode & 0o777, 0o600)
        self.assertNotIn(secret, (directory / "record.json").read_text())
        self.assertEqual(
            self.runtime.supply_secret(operation, secret), {"stored": True}
        )

    def test_secret_replacement_conflicts(self):
        self.runtime.prepare(self.manifest)
        operation = self.manifest["operation_id"]
        self.runtime.supply_secret(operation, "selected-private-secret-1234")
        with self.assertRaises(Conflict):
            self.runtime.supply_secret(operation, "different-private-secret-5678")

    def test_logs_reject_unbounded_ranges(self):
        for offset, limit in ((-1, 1), (0, 65537), (True, 1), (0, 0)):
            with self.subTest(offset=offset, limit=limit), self.assertRaises(Rejected):
                self.runtime.logs(self.manifest["operation_id"], offset, limit)

    def test_api_authenticates_prepare_and_rejects_extra_fields(self):
        with Server(("127.0.0.1", 0), Handler) as server:
            server.runtime, server.token = self.runtime, "a" * 64
            thread = threading.Thread(target=server.serve_forever)
            thread.start()
            client = Client(f"http://127.0.0.1:{server.server_port}", server.token)
            try:
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    urllib.request.urlopen(client.url + "/v1/capabilities")
                self.assertEqual(caught.exception.code, 401)
                self.assertEqual(client.prepare(self.manifest)["state"], "not-started")
                with self.assertRaises(ExecutorUnavailable):
                    client.request(
                        "/v1/prepare", {**self.manifest, "mounts": ["/host"]}
                    )
                self.assertEqual(
                    client.upload("context", self.context)["sha256"],
                    digest(self.context),
                )
            finally:
                server.shutdown()
                thread.join()

    def test_public_status_excludes_private_manifest_profile_and_secret_digest(self):
        self.runtime.prepare(self.manifest)
        operation = self.manifest["operation_id"]
        self.runtime.supply_secret(operation, "selected-private-secret-1234")
        self.assertEqual(
            self.runtime.status(operation),
            {"operation_id": operation, "state": "not-started"},
        )
