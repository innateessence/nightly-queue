import base64
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from .codex_task import auth_material, context_data, launch


def session_secret(*, expires=None):
    expiry = expires or int(datetime.now(timezone.utc).timestamp()) + 3600
    claims = (
        base64.urlsafe_b64encode(json.dumps({"exp": expiry}).encode())
        .decode()
        .rstrip("=")
    )
    return json.dumps(
        {
            "auth_mode": "chatgpt",
            "OPENAI_API_KEY": None,
            "tokens": {
                "access_token": "header." + claims + ".signature",
                "refresh_token": "refresh-token-not-for-tasks",
                "id_token": "id-token-0123456789",
                "account_id": "account-0123456789",
            },
        }
    )


class CodexTaskTests(unittest.TestCase):
    def setUp(self):
        self.context = {
            "schema_version": 1,
            "adapter": "codex",
            "model": "gpt-6.1-sol",
            "prompt": "Edit the fixture and test it.",
            "auth_kind": "token",
            "account_digest": "a" * 64,
        }

    def binary(self, root, *, version="0.169.0"):
        path = root / "codex"
        path.write_text(
            "#!/usr/bin/env python3\nimport sys,time\nif '--version' in sys.argv:\n print('codex-cli "
            + version
            + '\')\nelse:\n print(\'{"type":"turn.started"}\',flush=True)\n time.sleep(60)\n'
        )
        path.chmod(0o700)
        return str(path)

    def test_session_transport_never_includes_refresh_token(self):
        selected = session_secret()
        transported = auth_material("session", selected)
        self.assertNotIn("refresh-token-not-for-tasks", transported)
        self.assertEqual(json.loads(transported)["auth_mode"], "chatgptAuthTokens")
        self.assertEqual(json.loads(transported)["tokens"]["refresh_token"], "")

    def test_expired_session_is_rejected_without_login_or_refresh(self):
        with self.assertRaises(ValueError):
            auth_material("session", session_secret(expires=1))

    def test_runtime_cannot_receive_refreshable_session(self):
        with self.assertRaises(ValueError):
            auth_material("session", session_secret(), runtime=True)

    def test_prompt_cannot_supply_cli_configuration(self):
        with self.assertRaises(ValueError):
            context_data({**self.context, "argv": ["other"]})

    def test_fixed_launch_uses_clean_home_and_only_selected_auth(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch.dict(
                os.environ,
                {
                    "GITHUB_TOKEN": "not-for-agent",
                    "CODEX_HOME": "/host/private",
                    "OPENAI_API_KEY": "ambient-key",
                },
            ):
                arguments, environment, prompt = launch(
                    self.context,
                    "selected-key-0123456789",
                    home=root / "home",
                    binary=self.binary(root),
                )
            self.assertNotIn("GITHUB_TOKEN", environment)
            self.assertNotIn("OPENAI_API_KEY", environment)
            self.assertEqual(environment["CODEX_API_KEY"], "selected-key-0123456789")
            self.assertEqual(environment["CODEX_HOME"], str(root / "home/.codex"))
            self.assertEqual(prompt.read_text(), self.context["prompt"])
            self.assertIn("--ignore-user-config", arguments)
            self.assertIn("--ignore-rules", arguments)
            self.assertIn("--dangerously-bypass-approvals-and-sandbox", arguments)

    def test_wrong_installed_version_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaises(ValueError):
                launch(
                    self.context,
                    "selected-key-0123456789",
                    home=root / "home",
                    binary=self.binary(root, version="0.0.0"),
                )

    def test_fixture_process_hang_is_terminable(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            arguments, environment, prompt = launch(
                self.context,
                "selected-key-0123456789",
                home=root / "home",
                binary=self.binary(root),
            )
            with prompt.open("rb") as source:
                process = subprocess.Popen(
                    arguments, env=environment, stdin=source, stdout=subprocess.PIPE
                )
            try:
                self.assertEqual(
                    process.stdout.readline(), b'{"type":"turn.started"}\n'
                )
                process.terminate()
                self.assertLess(process.wait(timeout=5), 0)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
                process.stdout.close()
