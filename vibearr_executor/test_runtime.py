import os
import stat
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
from .runtime import Runtime, task_tmpfs
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

    def test_prepare_input_permissions_ignore_restrictive_service_umask(self):
        previous = os.umask(0o077)
        try:
            self.runtime.prepare(self.manifest)
        finally:
            os.umask(previous)
        operation = self.runtime.path(self.manifest["operation_id"])
        inputs = operation / "input"
        for path, expected in (
            (operation, 0o700),
            (inputs, 0o755),
            (inputs / "source.tar", 0o444),
            (inputs / "context", 0o444),
            (inputs / "secret", 0o600),
            (operation / "record.json", 0o600),
        ):
            with self.subTest(path=path.name):
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), expected)

    def test_changed_manifest_conflicts(self):
        self.runtime.prepare(self.manifest)
        with self.assertRaises(Conflict):
            self.runtime.prepare({**self.manifest, "context_digest": "c" * 64})

    def test_upload_rejects_digest_mismatch(self):
        with self.assertRaises(Rejected):
            self.runtime.upload("context", b"different", digest(self.context))

    def ordinary_manifest(self):
        from .contracts import Profile

        base = next(entry for entry in self.profiles if entry.phase == "validation")
        profile = Profile.parse(
            {
                **base.data(),
                "profile_id": "previous-validation",
                "argv": ["/usr/bin/true"],
            }
        )
        self.runtime.profiles[profile.profile_id] = profile
        manifest = {
            **self.manifest,
            "profile_id": profile.profile_id,
            "profile_digest": profile.digest,
            "phase": profile.phase,
            "network_hosts": [],
            "limits": asdict(profile.limits),
        }
        return profile, manifest

    def test_removed_prepared_profile_cannot_use_a_new_registry_qualification(self):
        profile, manifest = self.ordinary_manifest()
        self.runtime.prepare(manifest)
        self.runtime.profiles.pop(profile.profile_id)
        with (
            patch(
                "vibearr_executor.qualification.capabilities",
                return_value={"qualified": True},
            ),
            patch.object(self.runtime, "infrastructure") as infrastructure,
            self.assertRaises(Rejected),
        ):
            self.runtime.start(manifest["operation_id"])
        infrastructure.assert_not_called()

    def test_replaced_prepared_profile_cannot_launch_its_old_recipe(self):
        from .contracts import Profile

        profile, manifest = self.ordinary_manifest()
        self.runtime.prepare(manifest)
        self.runtime.profiles[profile.profile_id] = Profile.parse(
            {**profile.data(), "version": 2, "argv": ["/usr/bin/false"]}
        )
        with (
            patch(
                "vibearr_executor.qualification.capabilities",
                return_value={"qualified": True},
            ),
            patch.object(self.runtime, "infrastructure") as infrastructure,
            self.assertRaises(Rejected),
        ):
            self.runtime.start(manifest["operation_id"])
        infrastructure.assert_not_called()

    def test_run_owned_mounts_allow_exec_but_protected_state_does_not(self):
        mounts = task_tmpfs({"workspace_mb": 64})
        for path in ("/work", "/tmp", "/home/task"):
            flags = mounts[path].split(",")
            self.assertIn("exec", flags)
            self.assertIn("nosuid", flags)
            self.assertIn("nodev", flags)
            self.assertNotIn("noexec", flags)
        for path in ("/state", "/secrets"):
            self.assertIn("noexec", mounts[path].split(","))
            self.assertNotIn("exec", mounts[path].split(","))

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

    def test_unknown_operation_fences_a_new_operation_in_the_same_run(self):
        self.runtime.prepare(self.manifest)
        record = self.runtime.record(self.manifest["operation_id"])
        record.update(state="unknown", launch_intent=True)
        self.runtime.save(record)
        other = {**self.manifest, "operation_id": str(uuid4())}
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

    def test_stopped_init_reports_engine_evidence_without_inventing_a_task_result(self):
        record = self.running_record()
        engine_state = {
            "Running": False,
            "Status": "exited",
            "ExitCode": 137,
            "OOMKilled": True,
            "FinishedAt": "2026-11-16T10:00:00Z",
            "Error": "private-engine-detail",
        }
        with patch.object(self.runtime, "owned", return_value={"State": engine_state}):
            result = self.runtime.status(record["operation_id"])
        self.assertEqual(result["state"], "unknown")
        self.assertNotIn("result", result)
        self.assertEqual(
            result["container_state"],
            {
                "status": "exited",
                "exit_code": 137,
                "oom_killed": True,
                "finished_at": "2026-11-16T10:00:00Z",
            },
        )
        self.assertNotIn("private-engine-detail", str(result))
        self.assertTrue(self.runtime.record(record["operation_id"])["launch_intent"])

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

    def test_revoked_running_profile_can_reconcile_without_opening_a_gate(self):
        record = self.running_record()
        self.runtime.profiles.pop(record["profile"]["profile_id"])
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

    def test_revoked_pending_profile_never_opens_its_unconfirmed_gate(self):
        record = self.running_record()
        record["gate_opened"] = False
        self.runtime.save(record)
        self.runtime.profiles.pop(record["profile"]["profile_id"])
        with (
            patch.object(
                self.runtime, "owned", return_value={"State": {"Running": True}}
            ),
            patch.object(self.runtime, "harvest", return_value=False),
            patch.object(self.runtime.docker, "run") as engine,
        ):
            result = self.runtime.start(record["operation_id"])
        self.assertEqual(result["state"], "running")
        self.assertIn("no longer authorized", result["wait_reason"])
        engine.assert_not_called()

    def test_replacement_container_id_cannot_be_adopted(self):
        record = self.running_record()
        record["container_id"] = "original-container-id"
        self.runtime.save(record)
        container = {
            "Id": "replacement-container-id",
            "Config": {
                "Labels": {
                    "vibearr.executor": self.runtime.instance,
                    "vibearr.operation": record["operation_id"],
                }
            },
        }
        with patch.object(self.runtime.docker, "inspect", return_value=container):
            self.assertEqual(
                self.runtime.status(record["operation_id"])["state"], "unknown"
            )
            with self.assertRaises(Conflict):
                self.runtime.start(record["operation_id"])

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
