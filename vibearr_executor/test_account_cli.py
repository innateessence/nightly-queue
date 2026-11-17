import base64
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

from .account_bridge import AccountBridge, BridgeHandler, BRIDGE_PORT
from .account_client import AccountClient
from .account_leases import AccountLeases
from .account_server import AccountHandler
from .codex_account_rpc import AccountRPC
from .codex_task import BINARY, launch
from .contracts import canonical, digest
from .runtime import durable
from .secret_material import contains_secret, redact


def fixture_token(rotation):
    claims = {
        "sub": "fixture-user",
        "email": "account-fixture@example.test",
        "exp": 4102444800,
        "jti": str(rotation),
        "https://api.openai.com/auth": {
            "chatgpt_account_id": "fixture-account",
            "chatgpt_user_id": "fixture-user",
            "chatgpt_plan_type": "plus",
        },
    }
    parts = [
        canonical({"alg": "RS256", "typ": "JWT"}),
        canonical(claims),
        b"fixture-signature",
    ]
    return ".".join(
        base64.urlsafe_b64encode(part).decode().rstrip("=") for part in parts
    )


@unittest.skipUnless(
    Path(BINARY).is_file(),
    "Requires the pinned Docker agent image; no provider credentials or model calls.",
)
class PinnedAccountCLITests(unittest.TestCase):
    def serve(self, address, handler, **attributes):
        server = ThreadingHTTPServer(address, handler)
        for name, value in attributes.items():
            setattr(server, name, value)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.shutdown)
        return server

    def test_pinned_cli_renews_through_real_account_control_and_worker_bridge(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        calls = []

        def refresh(home, *, force):
            calls.append(force)
            value = json.loads((home / "auth.json").read_bytes())
            value["tokens"].update(
                access_token=fixture_token(len(calls)),
                id_token=fixture_token(len(calls)),
                refresh_token="provider-refresh-private-" + str(len(calls)),
            )
            durable(home / "auth.json", canonical(value))

        accounts = AccountLeases(root / "accounts", refresh)
        service = self.serve(
            ("127.0.0.1", 0),
            AccountHandler,
            accounts=accounts,
            token="fixture-control-capability-000000000000000",
        )
        control = AccountClient(
            f"http://127.0.0.1:{service.server_port}", service.token
        )
        worker = AccountClient(control.url)
        operation, boot = str(uuid4()), str(uuid4())
        directory, artifacts = root / operation, root / "artifacts"
        directory.mkdir(mode=0o700)
        artifacts.mkdir(mode=0o700)
        identity = {
            "runtime": "compose-slot-v2",
            "protocol_version": 2,
            "instance_id": str(uuid4()),
            "worker_boot": boot,
            "binding_digest": "d" * 64,
        }
        account = {
            "backend": "codex",
            "credential_id": 4,
            "credential_version": 1,
            "model": "gpt-6.1-sol",
        }
        context = {
            "schema_version": 1,
            "adapter": "codex",
            "model": account["model"],
            "auth_kind": "session",
            "account_digest": digest(canonical(account)),
            "prompt": "Offline authentication protocol fixture; no model call.",
        }
        context_digest = digest(canonical(context))
        durable(artifacts / context_digest, canonical(context))
        binding = {
            "operation_id": operation,
            "run_id": str(uuid4()),
            "source_digest": "a" * 64,
            "context_digest": context_digest,
            "account_digest": context["account_digest"],
            "credential": {"id": 4, "version": 1},
            "execution_identity": identity,
            "deadline": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
        }
        seed = {
            "auth_mode": "chatgpt",
            "tokens": {
                "access_token": fixture_token(0),
                "id_token": fixture_token(0),
                "refresh_token": "provider-refresh-private-initial",
                "account_id": "fixture-account",
            },
        }
        capsule = control.prepare(binding, canonical(seed).decode())
        record = {
            "operation_id": operation,
            "worker_boot": boot,
            "state": "running",
            "manifest": binding,
            "execution_identity": identity,
        }
        runtime = SimpleNamespace(
            active=operation,
            boot=boot,
            lock=threading.RLock(),
            record=lambda selected: record,
            path=lambda selected: directory,
            artifacts=artifacts,
        )
        bridge = AccountBridge(runtime, worker)
        bridge.bind(record, capsule)
        self.serve(("127.0.0.1", BRIDGE_PORT), BridgeHandler, bridge=bridge)
        _, environment, _ = launch(
            context, canonical(capsule).decode(), home=root / "task"
        )
        home = Path(environment["CODEX_HOME"])
        with patch(
            "vibearr_executor.codex_account_rpc.account_environment",
            return_value=environment,
        ):
            try:
                with AccountRPC(home, timeout=20) as cli:
                    cli.request(
                        1,
                        "initialize",
                        {
                            "clientInfo": {
                                "name": "vibearr-account-fixture",
                                "version": "1",
                            }
                        },
                    )
                    cli.send({"method": "initialized", "params": {}})
                    cli.request(2, "account/read", {"refreshToken": True})
            except ValueError:
                pass
        saved = json.loads((home / "auth.json").read_bytes())
        self.assertEqual(calls, [False, True])
        self.assertEqual(saved["tokens"]["access_token"], fixture_token(2))
        self.assertEqual(saved["tokens"]["refresh_token"], capsule["token"])
        self.assertNotIn("provider-refresh-private", json.dumps(saved))
        history = json.loads((directory / "account-protection.json").read_bytes())
        self.assertEqual(
            history[0]["tokens"]["access_token"], saved["tokens"]["access_token"]
        )
        protection = bridge.protection(operation, canonical(capsule), "")
        raw = saved["tokens"]["access_token"].encode()
        self.assertTrue(contains_secret(raw, protection))
        self.assertNotIn(raw, redact(raw, protection))
        bridge.close(operation)
        with self.assertRaises(ValueError):
            bridge.renew(capsule["token"])
