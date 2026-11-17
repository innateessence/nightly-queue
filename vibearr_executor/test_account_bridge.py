from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from uuid import uuid4

from .account_bridge import AccountBridge
from .account_leases import AccountLeases
from .codex_task import launch
from .contracts import canonical, digest
from .runtime import durable
from .secret_material import contains_secret, redact
from .test_codex_account import account_secret


class AccountBridgeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.operation = str(uuid4())
        operations, artifacts = self.root / "operations", self.root / "artifacts"
        operations.mkdir()
        artifacts.mkdir()
        self.directory = operations / self.operation
        self.directory.mkdir(mode=0o700)
        self.context = {
            "schema_version": 1,
            "adapter": "codex",
            "auth_kind": "session",
            "model": "gpt-6.1-sol",
            "prompt": "Test account renewal.",
            "account_digest": digest(
                canonical(
                    {
                        "backend": "codex",
                        "credential_id": 4,
                        "credential_version": 1,
                        "model": "gpt-6.1-sol",
                    }
                )
            ),
        }
        context_digest = digest(canonical(self.context))
        durable(artifacts / context_digest, canonical(self.context))
        identity = {
            "runtime": "compose-slot-v2",
            "protocol_version": 2,
            "instance_id": str(uuid4()),
            "worker_boot": str(uuid4()),
            "binding_digest": "d" * 64,
        }
        self.binding = {
            "operation_id": self.operation,
            "run_id": str(uuid4()),
            "source_digest": "a" * 64,
            "context_digest": context_digest,
            "deadline": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
            "execution_identity": identity,
            "account_digest": self.context["account_digest"],
            "credential": {"id": 4, "version": 1},
        }
        self.record = {
            "operation_id": self.operation,
            "manifest": self.binding.copy(),
            "execution_identity": identity,
            "worker_boot": identity["worker_boot"],
            "state": "running",
        }
        self.runtime = SimpleNamespace(
            lock=threading.RLock(),
            active=self.operation,
            boot=identity["worker_boot"],
            artifacts=artifacts,
            operations=operations,
            record=lambda operation: self.record,
            path=lambda operation: operations / operation,
        )
        self.broker = AccountLeases(self.root / "account", self.refresh)
        self.capsule = self.broker.prepare(
            {"binding": self.binding, "secret": account_secret()}
        )
        self.bridge = AccountBridge(self.runtime, self.broker)
        self.bridge.bind(self.record, self.capsule)

    def refresh(self, home, *, force):
        if not force:
            return
        account = json.loads((home / "auth.json").read_bytes())
        account["tokens"]["access_token"] += "-rotated"
        account["tokens"]["id_token"] = "rotated-private-id-token-000000"
        durable(home / "auth.json", canonical(account))

    def renew(self):
        return self.bridge.renew(self.capsule["token"])

    def test_renewal_returns_access_with_the_scoped_capability(self):
        response = self.renew()
        self.assertTrue(response["access_token"].endswith("-rotated"))
        self.assertEqual(response["refresh_token"], self.capsule["token"])
        self.assertNotIn("private-refresh-token", json.dumps(response))

    def test_forged_capability_cannot_refresh_the_account(self):
        with self.assertRaisesRegex(ValueError, "capability differs"):
            self.bridge.renew("forged-lease-capability-0000000000000")
        self.assertEqual(self.bridge.history[self.operation], [])

    def test_an_inactive_operation_cannot_refresh(self):
        self.runtime.active = ""
        with self.assertRaisesRegex(ValueError, "current running"):
            self.renew()

    def test_a_paused_operation_cannot_refresh(self):
        self.record["paused"] = True
        with self.assertRaisesRegex(ValueError, "current running"):
            self.renew()

    def test_an_expired_operation_cannot_refresh(self):
        self.record["manifest"]["deadline"] = (
            datetime.now(timezone.utc) - timedelta(seconds=1)
        ).isoformat()
        with self.assertRaisesRegex(ValueError, "deadline expired"):
            self.renew()

    def test_a_new_worker_boot_cannot_refresh(self):
        self.runtime.boot = str(uuid4())
        with self.assertRaisesRegex(ValueError, "another worker boot"):
            self.renew()

    def test_a_fenced_operation_cannot_refresh(self):
        self.bridge.fence(self.operation)
        with self.assertRaisesRegex(ValueError, "current running"):
            self.renew()

    def test_every_returned_rotation_is_durable_before_delivery(self):
        first, second = self.renew(), self.renew()
        history = json.loads((self.directory / "account-protection.json").read_bytes())
        self.assertEqual(
            [entry["tokens"]["access_token"] for entry in history],
            [first["access_token"], second["access_token"]],
        )

    def test_logs_redact_initial_and_all_renewed_tokens_and_capability(self):
        first, second = self.renew(), self.renew()
        tokens = [
            self.capsule["token"],
            self.capsule["auth"]["tokens"]["access_token"],
            first["access_token"],
            second["access_token"],
            second["id_token"],
        ]
        protection = self.bridge.protection(self.operation, canonical(self.capsule), "")
        for token in tokens:
            with self.subTest(token_type=token[:6]):
                self.assertNotIn(token.encode(), redact(token.encode(), protection))
                self.assertTrue(
                    contains_secret(b"candidate contains " + token.encode(), protection)
                )

    def test_positive_close_removes_the_local_capability(self):
        self.bridge.close(self.operation)
        self.assertFalse((self.directory / "account-lease.json").exists())
        self.assertTrue(self.broker.read(self.operation)["closed"])

    def test_close_failure_preserves_the_capability_for_reconciliation(self):
        class Unavailable:
            def close(self, *args):
                raise OSError("offline")

        self.bridge.client = Unavailable()
        self.bridge.close(self.operation)
        self.assertTrue((self.directory / "account-lease.json").exists())
        with self.assertRaisesRegex(ValueError, "current running"):
            self.renew()

    def test_invalid_close_material_does_not_interrupt_worker_cleanup(self):
        durable(self.directory / "account-lease.json", b"invalid JSON")
        self.bridge.close(self.operation)
        self.assertTrue((self.directory / "account-lease.json").exists())

    def test_restart_reconciles_only_positively_terminated_operations(self):
        self.record.update(state="exited", descendants_terminated=True)
        self.runtime.active = ""
        restarted = AccountBridge(self.runtime, self.broker)
        restarted.reconcile()
        self.assertTrue(self.broker.read(self.operation)["closed"])

    def test_renewal_finishing_after_termination_cannot_deliver_tokens(self):
        broker = self.broker

        def retire(*args):
            value = broker.renew(*args)
            self.bridge.fence(self.operation)
            return value

        self.bridge.client = SimpleNamespace(renew=retire)
        with self.assertRaisesRegex(ValueError, "current running"):
            self.renew()
        self.assertFalse((self.directory / "account-protection.json").exists())

    def test_task_launch_uses_managed_auth_with_only_a_scoped_refresh_lease(self):
        binary = self.root / "codex"
        binary.write_text("#!/bin/sh\nprintf 'codex-cli 0.169.0\\n'\n")
        binary.chmod(0o700)
        argv, environment, _ = launch(
            self.context,
            canonical(self.capsule).decode(),
            home=self.root / "task",
            binary=str(binary),
        )
        auth = json.loads((Path(environment["CODEX_HOME"]) / "auth.json").read_bytes())
        self.assertEqual(auth["auth_mode"], "chatgpt")
        self.assertEqual(auth["tokens"]["refresh_token"], self.capsule["token"])
        self.assertEqual(
            environment["CODEX_REFRESH_TOKEN_URL_OVERRIDE"],
            "http://127.0.0.1:8792/oauth/token",
        )
        self.assertNotIn("CODEX_API_KEY", environment)
        self.assertIn("--ignore-user-config", argv)
        self.assertIn("--ignore-rules", argv)
        self.assertIn("--strict-config", argv)
