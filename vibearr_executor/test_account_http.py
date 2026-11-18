from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import unittest
from uuid import uuid4
from datetime import datetime, timedelta, timezone

from .account_client import AccountClient, AccountUnavailable
from .account_leases import AccountLeases
from .account_server import AccountHandler
from .test_codex_account import account_secret


class AccountHTTPTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.broker = AccountLeases(Path(temporary.name), lambda home, force: None)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), AccountHandler)
        self.server.accounts = self.broker
        self.server.token = "private-control-token-000000000000000000"
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(thread.join, 5)
        self.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.client = AccountClient(self.url, self.server.token)
        self.binding = {
            "operation_id": str(uuid4()),
            "run_id": str(uuid4()),
            "source_digest": "a" * 64,
            "context_digest": "b" * 64,
            "account_digest": "c" * 64,
            "credential": {"id": 4, "version": 1},
            "deadline": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
            "execution_identity": {
                "runtime": "compose-slot-v2",
                "protocol_version": 2,
                "instance_id": str(uuid4()),
                "worker_boot": str(uuid4()),
                "binding_digest": "d" * 64,
            },
        }

    def test_private_service_reports_its_protocol(self):
        self.assertEqual(self.client.healthy()["runtime"], "codex-account-v1")

    def test_preparation_requires_the_dedicated_control_token(self):
        with self.assertRaises(AccountUnavailable):
            AccountClient(self.url).prepare(self.binding, account_secret())
        self.assertEqual(list(self.broker.leases.iterdir()), [])

    def test_registration_saves_an_expired_seed_without_provider_refresh(self):
        result = self.client.register(
            {"id": 4, "version": 1}, account_secret(expired=True)
        )
        self.assertEqual(
            result, {"registered": True, "credential": {"id": 4, "version": 1}}
        )
        self.assertEqual(list(self.broker.leases.iterdir()), [])
        self.assertTrue((self.broker.store.root / "4-1/codex/auth.json").exists())

    def test_registration_requires_the_dedicated_control_token(self):
        with self.assertRaises(AccountUnavailable):
            AccountClient(self.url).register({"id": 4, "version": 1}, account_secret())
        self.assertEqual(list(self.broker.store.root.iterdir()), [])

    def test_operation_capability_can_renew_and_close_without_control_token(self):
        capsule = self.client.prepare(self.binding, account_secret())
        worker = AccountClient(self.url)
        renewed = worker.renew(
            self.binding["operation_id"],
            capsule["token"],
            self.binding["execution_identity"],
            str(uuid4()),
        )
        self.assertEqual(renewed["auth"]["tokens"]["refresh_token"], "")
        self.assertTrue(
            worker.close(
                self.binding["operation_id"],
                capsule["token"],
                self.binding["execution_identity"],
            )["closed"]
        )

    def test_global_control_token_cannot_replace_an_operation_capability(self):
        self.client.prepare(self.binding, account_secret())
        with self.assertRaises(AccountUnavailable):
            self.client.renew(
                self.binding["operation_id"],
                self.server.token,
                self.binding["execution_identity"],
                str(uuid4()),
            )

    def test_public_or_credential_bearing_control_origins_are_rejected(self):
        for url in (
            "https://example.org",
            "http://attacker:8793",
            "http://secret@codex-account:8793",
            "http://codex-account:8793/?secret=token",
        ):
            with self.subTest(url=url), self.assertRaises(AccountUnavailable):
                AccountClient(url)

    def test_redirects_cannot_forward_private_account_control_authentication(self):
        seen = []

        class Redirect(AccountHandler):
            def do_GET(self):
                seen.append(self.path)
                self.send_response(302)
                self.send_header("Location", "/stolen")
                self.send_header("Content-Length", "0")
                self.send_header("Connection", "close")
                self.end_headers()

        self.server.RequestHandlerClass = Redirect
        with self.assertRaises(AccountUnavailable):
            self.client.healthy()
        self.assertEqual(seen, ["/health"])

    def test_provider_credentials_are_not_echoed_in_control_failures(self):
        seed = json.loads(account_secret())
        seed["auth_mode"] = "unsupported"
        with self.assertRaises(AccountUnavailable) as observed:
            self.client.prepare(self.binding, json.dumps(seed))
        self.assertNotIn(seed["tokens"]["refresh_token"], str(observed.exception))
