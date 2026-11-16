import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from vibearr_executor.opencode_task import auth_material, context_data, launch


CONNECTION = json.dumps(
    {"provider": "openai", "api_key": "fixture-openai-key-0123456789"}
)


def context():
    return {
        "schema_version": 1,
        "adapter": "opencode",
        "model": "openai/gpt-6.1-sol",
        "prompt": "Edit answer.py and run its test.",
        "auth_kind": "connection",
        "account_digest": "a" * 64,
    }


class OpenCodeTaskTests(unittest.TestCase):
    def launch(self, root):
        binary = root / "opencode"
        binary.write_text("#!/bin/sh\nprintf '1.19.5\\n'\n")
        binary.chmod(0o700)
        return launch(context(), CONNECTION, home=root / "home", binary=str(binary))

    def test_selected_connection_requires_official_endpoint(self):
        secret = json.dumps(
            {
                "provider": "openai",
                "api_key": "fixture-key-0123456789",
                "base_url": "http://localhost:8317/v1",
            }
        )
        with self.assertRaises(ValueError):
            auth_material("connection", secret)

    def test_selected_connection_rejects_other_provider_and_oauth(self):
        for value in (
            {"provider": "anthropic", "api_key": "fixture-key-0123456789"},
            {"provider": "openai", "api_key": "fixture-key-0123456789", "oauth": {}},
            {"provider": "ollama", "base_url": "http://localhost:11434"},
        ):
            with self.subTest(provider=value["provider"]):
                with self.assertRaises(ValueError):
                    auth_material("connection", json.dumps(value))

    def test_ambient_provider_credentials_and_configuration_are_discarded(self):
        ambient = {
            "OPENAI_API_KEY": "unselected-host-key",
            "ANTHROPIC_API_KEY": "unselected-anthropic",
            "OPENCODE_CONFIG_CONTENT": '{"model":"foreign"}',
            "OPENCODE_AUTH_CONTENT": "foreign",
            "AWS_PROFILE": "operator",
            "DATABASE_URL": "production",
            "VIBEARR_API_TOKEN": "queue-secret",
        }
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch.dict(os.environ, ambient),
        ):
            _, environment, _ = self.launch(Path(temporary))
        for key in ambient:
            self.assertNotIn(ambient[key], json.dumps(environment))

    def test_native_arguments_never_attach_or_resume_another_session(self):
        with tempfile.TemporaryDirectory() as temporary:
            arguments, _, prompt = self.launch(Path(temporary))
            self.assertEqual(
                arguments[1:],
                [
                    "run",
                    "--format",
                    "json",
                    "--auto",
                    "--model",
                    "openai/gpt-6.1-sol",
                    "--agent",
                    "build",
                    "--dir",
                    "/work",
                ],
            )
            self.assertEqual(prompt.read_text(), context()["prompt"])
            self.assertEqual(prompt.stat().st_mode & 0o777, 0o600)

    def test_configuration_cannot_enable_candidate_tools_or_interaction(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, environment, _ = self.launch(Path(temporary))
        config = json.loads(environment["OPENCODE_CONFIG_CONTENT"])
        self.assertEqual(config["permission"]["*"], "deny")
        self.assertEqual(config["permission"]["bash"], "allow")
        self.assertEqual(config["mcp"], {})
        self.assertEqual(config["plugin"], [])
        self.assertEqual(config["enabled_providers"], ["openai"])
        self.assertEqual(config["model"], config["small_model"])
        self.assertFalse(config["lsp"])
        self.assertFalse(config["formatter"])
        self.assertEqual(environment["OPENCODE_DISABLE_PROJECT_CONFIG"], "1")
        self.assertEqual(environment["OPENCODE_DISABLE_DEFAULT_PLUGINS"], "1")

    def test_all_native_state_is_in_private_home(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, environment, _ = self.launch(root)
            for key in (
                "XDG_CONFIG_HOME",
                "XDG_DATA_HOME",
                "XDG_CACHE_HOME",
                "XDG_STATE_HOME",
                "TMPDIR",
                "OPENCODE_DB",
            ):
                self.assertTrue(Path(environment[key]).is_relative_to(root / "home"))
            selected = json.loads(environment["OPENCODE_AUTH_CONTENT"])
            self.assertEqual(set(selected), {"openai"})
            self.assertEqual(selected["openai"]["type"], "api")

    def test_context_cannot_select_other_provider_or_authentication(self):
        for field, value in (
            ("model", "anthropic/claude"),
            ("auth_kind", "session"),
            ("adapter", "codex"),
        ):
            with self.subTest(field=field):
                candidate = context()
                candidate[field] = value
                with self.assertRaises(ValueError):
                    context_data(candidate)

    def test_context_cannot_supply_launch_configuration(self):
        candidate = context()
        candidate["arguments"] = ["--attach", "http://localhost:4096"]
        with self.assertRaises(ValueError):
            context_data(candidate)

    def test_installed_version_mismatch_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            binary = root / "opencode"
            binary.write_text("#!/bin/sh\nprintf '2.0.0\\n'\n")
            binary.chmod(0o700)
            with self.assertRaises(ValueError):
                launch(context(), CONNECTION, home=root / "home", binary=str(binary))

    def test_disposable_database_uses_only_executor_generated_marker(self):
        marker = "postgresql://task:" + "a" * 48 + "@172.22.0.5:5432/task"
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch.dict(os.environ, {"VIBEARR_DISPOSABLE_DATABASE_URL": marker}),
        ):
            _, environment, _ = self.launch(Path(temporary))
        self.assertEqual(environment["DATABASE_URL"], marker)
        self.assertEqual(environment["VIBEARR_POSTGRES_HOST"], "172.22.0.5")
