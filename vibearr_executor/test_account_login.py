import fcntl
import json
from pathlib import Path
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from .account_leases import AccountLeases
from .account_login import login
from .contracts import canonical
from .runtime import durable
from .test_codex_account import account_secret


class AccountLoginTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.accounts = AccountLeases(self.root / "account", lambda home, force: None)
        self.reference = {"id": 4, "version": 1}
        self.seed = account_secret()
        self.accounts.register(self.reference, self.seed)
        self.binary = self.root / "codex"
        self.fake_cli(account_secret())

    def fake_cli(self, content, *, version="0.169.0"):
        self.binary.write_text(
            "#!/usr/bin/env python3\nimport os,sys\nfrom pathlib import Path\nif '--version' in sys.argv:\n print('codex-cli "
            + version
            + "')\nelse:\n assert sys.argv[-2:] == ['login', '--device-auth']\n assert not Path(os.environ['CODEX_HOME'], 'auth.json').exists()\n Path(os.environ['CODEX_HOME'], 'auth.json').write_text("
            + repr(content)
            + ")\n"
        )
        self.binary.chmod(0o700)

    def sign_in(self):
        return login(self.accounts.root, self.reference, binary=str(self.binary))

    def test_device_login_updates_the_same_private_account_without_an_api_key(self):
        result = self.sign_in()
        self.assertEqual(
            result,
            {
                "account_login": "saved",
                "credential": self.reference,
                "api_key_required": False,
            },
        )
        self.assertTrue(
            (self.accounts.store.root / "4-1/login-observation.json").exists()
        )

    def test_service_must_be_stopped_before_login(self):
        with (self.accounts.root / "service.lock").open("a") as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(ValueError, "stop codex-account"):
                self.sign_in()

    def test_unterminated_operations_prevent_device_login_even_after_deadline(self):
        binding = {
            "operation_id": str(uuid4()),
            "run_id": str(uuid4()),
            "source_digest": "a" * 64,
            "context_digest": "b" * 64,
            "account_digest": "c" * 64,
            "credential": self.reference,
            "deadline": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
            "execution_identity": {
                "runtime": "compose-slot-v2",
                "protocol_version": 2,
                "instance_id": str(uuid4()),
                "worker_boot": str(uuid4()),
                "binding_digest": "d" * 64,
            },
        }
        self.accounts.prepare({"binding": binding, "secret": self.seed})
        path = self.accounts.leases / binding["operation_id"]
        record = json.loads(path.read_bytes())
        record["binding"]["deadline"] = (
            datetime.now(timezone.utc) - timedelta(minutes=5)
        ).isoformat()
        durable(path, canonical(record))
        with self.assertRaisesRegex(ValueError, "positive termination"):
            self.sign_in()

    def test_signing_into_another_workspace_does_not_authorize_the_selected_connection(
        self,
    ):
        self.fake_cli(account_secret(account="another-workspace"))
        with self.assertRaisesRegex(ValueError, "same selected account"):
            self.sign_in()
        self.assertFalse(
            (self.accounts.store.root / "4-1/login-observation.json").exists()
        )
        saved = json.loads(
            (self.accounts.store.root / "4-1/codex/auth.json").read_bytes()
        )
        self.assertEqual(saved, json.loads(self.seed))
        self.assertEqual(
            list((self.accounts.store.root / "4-1").glob(".device-login-*")), []
        )

    def test_only_a_fresh_device_login_authorizes_this_restored_account(self):
        marker = {"backup_id": str(uuid4()), "authority": str(uuid4())}
        durable(self.accounts.root / "restore-required.json", canonical(marker))
        self.sign_in()
        authorized = json.loads(
            (self.accounts.store.root / "4-1/restore-authorization.json").read_bytes()
        )
        self.assertEqual(authorized, marker)
        self.assertTrue((self.accounts.root / "restore-required.json").exists())

    def test_an_unreviewed_cli_cannot_replace_the_account_login(self):
        self.fake_cli(account_secret(), version="0.0.0")
        with self.assertRaisesRegex(ValueError, "pinned Codex CLI"):
            self.sign_in()
