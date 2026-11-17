import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from .managed_login import collect, install_login, read_login, save_login, validate
from .native_task import command
from .ollama_relay import external_endpoint


class ExternalOllamaTests(unittest.TestCase):
    def test_explicit_external_dns_endpoint_is_supported(self):
        self.assertEqual(
            external_endpoint("https://ollama.example:11434/api"),
            "https://ollama.example:11434",
        )

    def test_explicit_private_endpoint_is_supported(self):
        self.assertEqual(
            external_endpoint("http://192.168.1.20:11434"), "http://192.168.1.20:11434"
        )

    def test_internal_and_metadata_endpoints_are_rejected(self):
        for endpoint in (
            "http://127.0.0.1:11434",
            "http://169.254.169.254",
            "http://postgres:5432",
            "http://app:8000",
            "http://runner-ollama:11434",
            "http://localhost:11434",
        ):
            with self.subTest(endpoint=endpoint), self.assertRaises(ValueError):
                external_endpoint(endpoint)

    def test_endpoint_cannot_inject_paths_credentials_or_query(self):
        for endpoint in (
            "https://user:secret@ollama.example",
            "http://ollama.example/api/pull",
            "http://ollama.example?model=other",
            "http://ollama.example#fragment",
        ):
            with self.subTest(endpoint=endpoint), self.assertRaises(ValueError):
                external_endpoint(endpoint)


class ManagedLoginTests(unittest.TestCase):
    def codex(self):
        return {
            ".codex/auth.json": {
                "auth_mode": "chatgpt",
                "tokens": {
                    "access_token": "access",
                    "refresh_token": "refresh",
                    "id_token": "identity",
                    "account_id": "account",
                },
            }
        }

    def test_login_roundtrip_preserves_refresh_material(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch("vibearr_executor.managed_login.ROOT", Path(directory)),
        ):
            save_login("codex", 1, self.codex())
            self.assertEqual(read_login("codex", 1), self.codex())

    def test_accounts_do_not_share_login_files(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch("vibearr_executor.managed_login.ROOT", Path(directory)),
        ):
            save_login("codex", 1, self.codex())
            with self.assertRaises(FileNotFoundError):
                read_login("codex", 2)

    def test_codex_api_key_is_rejected(self):
        files = self.codex()
        files[".codex/auth.json"]["OPENAI_API_KEY"] = "api-key"
        with self.assertRaises(ValueError):
            validate("codex", files)

    def test_claude_api_credentials_are_rejected(self):
        with self.assertRaises(ValueError):
            validate("claude", {".claude/.credentials.json": {"apiKey": "api-key"}})

    def test_collect_rejects_symlinked_auth_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "home").mkdir()
            (root / "elsewhere").mkdir()
            install_login("codex", self.codex(), root / "elsewhere")
            (root / "home/.codex").symlink_to(root / "elsewhere/.codex")
            with self.assertRaises(OSError):
                collect("codex", root / "home")

    def test_install_rejects_non_auth_files(self):
        files = {**self.codex(), ".codex/config.toml": {"hooks": "untrusted"}}
        with tempfile.TemporaryDirectory() as directory, self.assertRaises(ValueError):
            install_login("codex", files, Path(directory))


class NativeCommandTests(unittest.TestCase):
    def test_claude_subscription_mode_does_not_use_bare_api_mode(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict("os.environ", {}, clear=True),
        ):
            arguments = command("claude", "claude-sonnet-5-5", Path(directory))
        self.assertNotIn("--bare", arguments)
        self.assertIn("bypassPermissions", arguments)
        self.assertIn("--strict-mcp-config", arguments)

    def test_opencode_has_only_external_ollama_provider(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict("os.environ", {}, clear=True),
        ):
            arguments = command("opencode", "qwen3-coder:30b", Path(directory))
            config = json.loads((Path(directory) / "opencode.json").read_text())
        self.assertIn("--pure", arguments)
        self.assertEqual(config["enabled_providers"], ["ollama"])
        self.assertEqual(config["model"], "ollama/qwen3-coder:30b")
        self.assertNotIn("apiKey", json.dumps(config))
