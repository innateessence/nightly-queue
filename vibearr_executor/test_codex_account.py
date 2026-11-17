import base64
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest

from .codex_account import AccountStore, managed_auth, private_bytes


def account_secret(*, account="test-account", expired=False):
    expires = datetime.now(timezone.utc) + timedelta(hours=-1 if expired else 8)
    claims = (
        base64.urlsafe_b64encode(json.dumps({"exp": int(expires.timestamp())}).encode())
        .decode()
        .rstrip("=")
    )
    return json.dumps(
        {
            "auth_mode": "chatgpt",
            "tokens": {
                "access_token": "header." + claims + ".signature",
                "refresh_token": "private-refresh-token-000000000",
                "id_token": "private-id-token-00000000000",
                "account_id": account,
            },
            "last_refresh": "2026-01-01T00:00:00Z",
        }
    )


class ManagedAccountStoreTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.reference = {"id": 4, "version": 1}
        self.seed = account_secret()
        self.refreshed = []

    def refresh(self, home, *, force):
        self.refreshed.append(force)
        value = json.loads((home / "auth.json").read_bytes())
        value["tokens"]["refresh_token"] = "rotated-private-refresh-token-00000"
        (home / "auth.json").write_text(json.dumps(value))

    def test_material_contains_access_without_the_refresh_credential(self):
        store = AccountStore(self.root, self.refresh)
        material = json.loads(store.material(self.reference, self.seed))
        self.assertEqual(material["auth_mode"], "chatgptAuthTokens")
        self.assertEqual(material["tokens"]["refresh_token"], "")
        self.assertNotIn("rotated-private-refresh-token", json.dumps(material))

    def test_rotated_login_is_preserved_when_original_seed_is_supplied_again(self):
        store = AccountStore(self.root, self.refresh)
        store.material(self.reference, self.seed)
        observed = []

        def observe(home, *, force):
            observed.append(
                json.loads((home / "auth.json").read_bytes())["tokens"]["refresh_token"]
            )

        store = AccountStore(self.root, observe)
        store.material(self.reference, self.seed)
        self.assertEqual(observed, ["rotated-private-refresh-token-00000"])

    def test_account_storage_is_private(self):
        store = AccountStore(self.root, self.refresh)
        store.material(self.reference, self.seed)
        path = self.root / "4-1" / "codex" / "auth.json"
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)

    def test_another_seed_requires_a_new_credential_version(self):
        store = AccountStore(self.root, self.refresh)
        store.material(self.reference, self.seed)
        changed = json.loads(self.seed)
        changed["tokens"]["refresh_token"] = "replacement-private-refresh-token-000"
        with self.assertRaisesRegex(ValueError, "new credential version"):
            store.material(self.reference, json.dumps(changed))

    def test_new_credential_version_does_not_reuse_old_login(self):
        store = AccountStore(self.root, self.refresh)
        store.material(self.reference, self.seed)
        store.material({"id": 4, "version": 2}, self.seed)
        self.assertTrue((self.root / "4-1" / "codex" / "auth.json").exists())
        self.assertTrue((self.root / "4-2" / "codex" / "auth.json").exists())

    def test_refresh_cannot_switch_the_selected_account(self):
        def switch(home, *, force):
            (home / "auth.json").write_text(account_secret(account="other-account"))

        store = AccountStore(self.root, switch)
        with self.assertRaisesRegex(ValueError, "different account"):
            store.material(self.reference, self.seed)

    def test_expired_seed_can_be_repaired_by_managed_cli_refresh(self):
        def renew(home, *, force):
            (home / "auth.json").write_text(account_secret())

        store = AccountStore(self.root, renew)
        material = store.material(self.reference, account_secret(expired=True))
        self.assertEqual(json.loads(material)["auth_mode"], "chatgptAuthTokens")

    def test_unauthorized_recovery_can_request_forced_cli_refresh(self):
        store = AccountStore(self.root, self.refresh)
        store.material(self.reference, self.seed, force=True)
        self.assertEqual(self.refreshed, [True])

    def test_api_key_file_cannot_seed_the_account_store(self):
        with self.assertRaisesRegex(ValueError, "managed ChatGPT"):
            managed_auth(b'{"auth_mode":"apikey","OPENAI_API_KEY":"not-used"}')

    def test_seed_cannot_select_an_oauth_issuer(self):
        seed = json.loads(self.seed)
        seed["tokens"]["oauth_provenance"] = {
            "issuer": "https://attacker.example",
            "client_id": "attacker",
        }
        with self.assertRaisesRegex(ValueError, "managed ChatGPT"):
            managed_auth(json.dumps(seed).encode())

    def test_public_account_directory_is_rejected(self):
        self.root.chmod(0o755)
        with self.assertRaisesRegex(ValueError, "private"):
            AccountStore(self.root, self.refresh)

    def test_linked_account_file_is_rejected(self):
        original = self.root / "auth.json"
        original.write_text(self.seed)
        original.chmod(0o600)
        linked = self.root / "linked.json"
        linked.symlink_to(original)
        with self.assertRaises(OSError):
            private_bytes(linked)
