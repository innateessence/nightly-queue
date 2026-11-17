import copy
import importlib.util
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from uuid import uuid4

from vibearr_runner.native_codex import NativeCodexAdapter, codex_profile
from vibearr_runner.native_sessions import NativeSessionError
from vibearr_runner.policy import default_execution_policy

from .test_native_codex import ExecutorFixture

EXECUTOR_INSTALLED = importlib.util.find_spec("vibearr_executor") is not None
if EXECUTOR_INSTALLED:
    from vibearr_executor.account_leases import AccountLeases
    from vibearr_executor.artifacts import pack
    from vibearr_executor.runtime import durable
    from vibearr_executor.test_codex_account import account_secret


class AccountExecutorFixture(ExecutorFixture):
    def __init__(self):
        super().__init__()
        self.profile = codex_profile(self.profile.image, "session")
        self.identity = {
            "runtime": "compose-slot-v2",
            "protocol_version": 2,
            "instance_id": str(uuid4()),
            "worker_boot": str(uuid4()),
            "binding_digest": self.binding_digest,
        }

    def capabilities(self):
        return {
            **super().capabilities(),
            "account_auth": {"lease_protocol": 1, "bridge_port": 8792},
        }

    def prepare(self, manifest):
        return {**super().prepare(manifest), "execution_identity": self.identity}


@unittest.skipUnless(
    EXECUTOR_INSTALLED, "Install the native-runtime extra for account lease tests."
)
class NativeAccountTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.broker = AccountLeases(Path(temporary.name), self.refresh)
        self.accounts = SimpleNamespace(
            prepare=lambda binding, secret: self.broker.prepare(
                {"binding": binding, "secret": secret}
            )
        )
        self.client = AccountExecutorFixture()
        self.policy = default_execution_policy()
        self.policy["runtime_profile"] = self.client.profile.profile_id
        self.request = {
            "run_id": str(uuid4()),
            "operation_id": str(uuid4()),
            "source": pack({"answer.py": (0o644, b"answer = 0\n")}),
            "prompt": "Set answer to 42.",
            "model": "gpt-6.1-sol",
            "policy": self.policy,
            "deadline": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
            "credential": {
                "credential": {
                    "id": 4,
                    "version": 1,
                    "kind": "session",
                    "provider": "codex",
                },
                "material": {"secret": account_secret(expired=True)},
            },
        }

    def refresh(self, home, *, force):
        durable(home / "auth.json", account_secret().encode())

    def test_expired_saved_login_renews_in_protected_storage(self):
        adapter = NativeCodexAdapter(self.client, account_client=self.accounts)
        session = adapter.prepare(**self.request)
        capsule = json.loads(self.client.secrets[self.request["operation_id"]])
        self.assertEqual(capsule["binding"]["execution_identity"], self.client.identity)
        self.assertEqual(capsule["binding"]["credential"], {"id": 4, "version": 1})
        self.assertEqual(capsule["auth"]["tokens"]["refresh_token"], "")
        self.assertNotIn("private-refresh-token", json.dumps(session.to_data()))

    def test_repeated_prepare_preserves_the_exact_worker_secret(self):
        adapter = NativeCodexAdapter(self.client, account_client=self.accounts)
        adapter.prepare(**self.request)
        first = self.client.secrets[self.request["operation_id"]]
        adapter.prepare(**self.request)
        self.assertEqual(first, self.client.secrets[self.request["operation_id"]])

    def test_real_account_worker_requires_protected_account_control(self):
        adapter = NativeCodexAdapter(self.client, account_client=None)
        self.request["credential"]["material"]["secret"] = account_secret()
        with self.assertRaisesRegex(NativeSessionError, "protected Docker"):
            adapter.prepare(**self.request)

    def test_a_broker_cannot_substitute_another_workers_lease(self):
        broker = self.broker

        class Changed:
            def prepare(self, binding, secret):
                value = copy.deepcopy(
                    broker.prepare({"binding": binding, "secret": secret})
                )
                value["binding"]["execution_identity"]["worker_boot"] = str(uuid4())
                return value

        adapter = NativeCodexAdapter(self.client, account_client=Changed())
        with self.assertRaisesRegex(NativeSessionError, "another operation"):
            adapter.prepare(**self.request)

    def test_a_broker_cannot_substitute_another_accounts_access(self):
        broker = self.broker

        class Changed:
            def prepare(self, binding, secret):
                value = broker.prepare({"binding": binding, "secret": secret})
                value["auth"]["tokens"]["account_id"] = "other-account"
                return value

        adapter = NativeCodexAdapter(self.client, account_client=Changed())
        with self.assertRaisesRegex(NativeSessionError, "another account"):
            adapter.prepare(**self.request)
