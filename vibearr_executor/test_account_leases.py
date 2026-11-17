from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

from .account_leases import AccountLeases, lease_secret
from .contracts import canonical
from .test_codex_account import account_secret


class AccountLeaseTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.now = datetime.now(timezone.utc).timestamp()
        self.calls = []
        self.broker = AccountLeases(self.root, self.refresh, clock=lambda: self.now)
        self.binding = {
            "operation_id": str(uuid4()),
            "run_id": str(uuid4()),
            "source_digest": "a" * 64,
            "context_digest": "b" * 64,
            "account_digest": "c" * 64,
            "credential": {"id": 4, "version": 1},
            "deadline": datetime.fromtimestamp(
                self.now + 600, timezone.utc
            ).isoformat(),
            "execution_identity": {
                "runtime": "compose-slot-v2",
                "protocol_version": 2,
                "instance_id": str(uuid4()),
                "worker_boot": str(uuid4()),
                "binding_digest": "d" * 64,
            },
        }
        self.request = {"binding": self.binding, "secret": account_secret()}

    def refresh(self, home, *, force):
        self.calls.append(force)
        path = home / "auth.json"
        document = json.loads(path.read_bytes())
        document["tokens"]["refresh_token"] = "provider-refresh-token-private-" + str(
            len(self.calls)
        )
        document["tokens"]["access_token"] = (
            document["tokens"]["access_token"].rsplit(".", 1)[0]
            + ".rotation"
            + str(len(self.calls))
        )
        path.write_bytes(canonical(document))

    def prepare(self):
        return self.broker.prepare(self.request)

    def renew(self, capsule, request_id=None, *, identity=None, token=None):
        return self.broker.renew(
            self.binding["operation_id"],
            token or capsule["token"],
            identity or self.binding["execution_identity"],
            request_id or str(uuid4()),
        )

    def test_prepared_capability_never_contains_the_provider_refresh_token(self):
        capsule = self.prepare()
        self.assertEqual(lease_secret(capsule), capsule)
        self.assertEqual(capsule["auth"]["tokens"]["refresh_token"], "")
        self.assertNotIn("provider-refresh-token-private", json.dumps(capsule))

    def test_repeated_prepare_returns_the_same_secret_bytes(self):
        first = self.prepare()
        self.assertEqual(canonical(first), canonical(self.prepare()))
        self.assertEqual(self.calls, [False])

    def test_changed_worker_cannot_rebind_an_existing_operation(self):
        self.prepare()
        self.binding["execution_identity"]["worker_boot"] = str(uuid4())
        with self.assertRaisesRegex(ValueError, "another credential or worker"):
            self.prepare()

    def test_renewal_retries_do_not_repeat_provider_refresh(self):
        capsule = self.prepare()
        identity = str(uuid4())
        first = self.renew(capsule, identity)
        self.assertEqual(first, self.renew(capsule, identity))
        self.assertEqual(self.calls, [False, True])

    def test_new_renewal_uses_the_managed_rotated_login(self):
        capsule = self.prepare()
        renewed = self.renew(capsule)
        self.assertNotEqual(
            renewed["auth"]["tokens"]["access_token"],
            capsule["auth"]["tokens"]["access_token"],
        )
        self.assertEqual(renewed["auth"]["tokens"]["refresh_token"], "")

    def test_wrong_capability_is_rejected_before_refresh(self):
        capsule = self.prepare()
        with self.assertRaisesRegex(ValueError, "capability differs"):
            self.renew(capsule, token="forged-capability-000000000000000000000")
        self.assertEqual(self.calls, [False])

    def test_other_worker_boot_cannot_renew_the_account(self):
        capsule = self.prepare()
        worker = {**self.binding["execution_identity"], "worker_boot": str(uuid4())}
        with self.assertRaisesRegex(ValueError, "another worker boot"):
            self.renew(capsule, identity=worker)

    def test_expired_operation_cannot_renew_the_account(self):
        capsule = self.prepare()
        self.now += 601
        with self.assertRaisesRegex(ValueError, "expired"):
            self.renew(capsule)

    def test_terminated_operation_cannot_renew_the_account(self):
        capsule = self.prepare()
        self.broker.close(
            self.binding["operation_id"],
            capsule["token"],
            self.binding["execution_identity"],
        )
        with self.assertRaisesRegex(ValueError, "retired"):
            self.renew(capsule)

    def test_service_restart_preserves_the_exact_existing_lease(self):
        capsule = self.prepare()
        restarted = AccountLeases(self.root, self.refresh, clock=lambda: self.now)
        self.assertEqual(restarted.prepare(self.request), capsule)

    def test_new_authority_fences_restored_lease_capabilities(self):
        capsule = self.prepare()
        (self.root / "authority").write_text(str(uuid4()))
        self.broker = AccountLeases(self.root, self.refresh, clock=lambda: self.now)
        with self.assertRaisesRegex(ValueError, "retired"):
            self.renew(capsule)

    def test_restore_requires_new_account_sign_in(self):
        marker = self.root / "restore-required.json"
        marker.write_bytes(canonical({"backup_id": str(uuid4())}))
        marker.chmod(0o600)
        with self.assertRaisesRegex(ValueError, "Sign in again"):
            self.prepare()

    def test_only_expired_positively_closed_leases_can_be_pruned(self):
        capsule = self.prepare()
        self.now += 601
        self.broker.prune()
        self.assertTrue((self.broker.leases / self.binding["operation_id"]).exists())
        self.broker.close(
            self.binding["operation_id"],
            capsule["token"],
            self.binding["execution_identity"],
        )
        self.broker.prune()
        self.assertFalse((self.broker.leases / self.binding["operation_id"]).exists())
