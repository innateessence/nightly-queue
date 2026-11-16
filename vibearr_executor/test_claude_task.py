import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from .claude_task import ARGV, TOOLS, VERSION, auth_material, context_data, launch


class ClaudeTaskTests(unittest.TestCase):
    def context(self):
        return {
            "schema_version": 1,
            "adapter": "claude",
            "model": "claude-sonnet-5-5",
            "prompt": "Edit answer.py and run its tests.",
            "auth_kind": "token",
            "account_digest": "a" * 64,
        }

    def binary(self, root, version=VERSION, delay=0):
        path = root / "claude"
        path.write_text(
            "#!/usr/bin/env python3\nimport sys,time\n"
            + f"time.sleep({delay})\n"
            + f"print('{version} (Claude Code)')\n"
        )
        path.chmod(0o755)
        return str(path)

    def test_fixed_wrapper_uses_stdin_clean_home_and_noninteractive_tool_permissions(
        self,
    ):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            context = self.context()
            arguments, environment, prompt = launch(
                context,
                "fixture-key-0123456789",
                home=root / "home",
                binary=self.binary(root),
            )
            self.assertEqual(prompt.read_text(), context["prompt"])
            self.assertNotIn(context["prompt"], arguments)
            self.assertNotIn("fixture-key-0123456789", json.dumps(arguments))
            self.assertEqual(environment["ANTHROPIC_API_KEY"], "fixture-key-0123456789")
            for flag in (
                "--bare",
                "--safe-mode",
                "--disable-slash-commands",
                "--print",
                "--verbose",
                "--strict-mcp-config",
                "--no-session-persistence",
            ):
                self.assertIn(flag, arguments)
            self.assertEqual(arguments[arguments.index("--tools") + 1], ",".join(TOOLS))
            self.assertEqual(
                arguments[arguments.index("--permission-mode") + 1], "dontAsk"
            )
            self.assertEqual(
                arguments[arguments.index("--permission-prompts") + 1], "none"
            )
            self.assertEqual(arguments[arguments.index("--setting-sources") + 1], "")
            self.assertEqual(
                json.loads(arguments[arguments.index("--mcp-config") + 1]),
                {"mcpServers": {}},
            )
            self.assertEqual(
                environment["CLAUDE_CONFIG_DIR"], str(root / "home/.claude")
            )
            self.assertEqual((root / "home/.claude").stat().st_mode & 0o777, 0o700)
            self.assertEqual(prompt.stat().st_mode & 0o777, 0o600)
            self.assertEqual(ARGV[-1], "vibearr_executor.claude_task")

    def test_host_auth_provider_endpoint_hooks_and_git_secrets_are_not_inherited(self):
        ambient = {
            "CLAUDE_CODE_OAUTH_TOKEN": "host-oauth",
            "ANTHROPIC_AUTH_TOKEN": "host-auth",
            "ANTHROPIC_BASE_URL": "https://foreign.invalid",
            "CLAUDE_CODE_USE_BEDROCK": "1",
            "AWS_SECRET_ACCESS_KEY": "host-aws",
            "GITHUB_TOKEN": "host-github",
            "VIBEARR_API_TOKEN": "host-api",
            "VIBEARR_CREDENTIAL_KEY": "host-encryption",
            "CLAUDE_CONFIG_DIR": "/host/config",
            "DATABASE_URL": "postgresql://production",
            "BASH_ENV": "/host/hook",
            "PYTHONPATH": "/host/helpers",
        }
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch.dict(os.environ, ambient),
        ):
            root = Path(temporary)
            _, environment, _ = launch(
                self.context(),
                "fixture-key-0123456789",
                home=root / "home",
                binary=self.binary(root),
            )
            for key, value in ambient.items():
                self.assertNotEqual(environment.get(key), value, key)
            self.assertEqual(environment["GIT_CONFIG_GLOBAL"], "/dev/null")
            self.assertEqual(environment["ENABLE_CLAUDEAI_MCP_SERVERS"], "false")
            self.assertEqual(environment["DISABLE_UPDATES"], "1")

    def test_missing_api_key_or_subscription_cannot_use_ambient_login(self):
        for kind, key in (
            ("token", ""),
            ("session", '{"access_token":"host-token"}'),
            ("token", "sk-ant-oat01-01234567890123456789"),
        ):
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                auth_material(kind, key)

    def test_candidate_context_cannot_supply_flags_auth_helpers_or_settings(self):
        for field in (
            "argv",
            "settings",
            "apiKeyHelper",
            "plugins",
            "mcpServers",
            "endpoint",
            "session_id",
        ):
            context = {**self.context(), field: "untrusted"}
            with self.subTest(field=field), self.assertRaises(ValueError):
                context_data(context)

    def test_explicit_model_rejects_aliases_urls_and_cli_arguments(self):
        for model in (
            "sonnet",
            "https://example.invalid",
            "--model=foreign",
            "claude-" + "a" * 200,
        ):
            with self.subTest(model=model), self.assertRaises(ValueError):
                context_data({**self.context(), "model": model})

    def test_nonmatching_installed_version_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaisesRegex(ValueError, "pinned version"):
                launch(
                    self.context(),
                    "fixture-key-0123456789",
                    home=root / "home",
                    binary=self.binary(root, "2.1.118"),
                )

    def test_hanging_version_probe_has_bounded_timeout(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch(
                "vibearr_executor.claude_task.subprocess.run",
                side_effect=subprocess.TimeoutExpired("claude", 5),
            ):
                with self.assertRaises(subprocess.TimeoutExpired):
                    launch(
                        self.context(),
                        "fixture-key-0123456789",
                        home=root / "home",
                        binary=self.binary(root),
                    )

    def test_existing_home_configuration_is_not_reused(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "home/.claude").mkdir(parents=True)
            with self.assertRaises(FileExistsError):
                launch(
                    self.context(),
                    "fixture-key-0123456789",
                    home=root / "home",
                    binary=self.binary(root),
                )

    def test_only_executor_disposable_database_connection_is_added(self):
        database = "postgresql://task:" + "b" * 48 + "@10.80.0.3:5432/task"
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch.dict(os.environ, {"VIBEARR_DISPOSABLE_DATABASE_URL": database}),
        ):
            root = Path(temporary)
            _, environment, _ = launch(
                self.context(),
                "fixture-key-0123456789",
                home=root / "home",
                binary=self.binary(root),
            )
            self.assertEqual(environment["DATABASE_URL"], database)
            self.assertEqual(environment["POSTGRES_DB"], "task")
            self.assertEqual(environment["NO_PROXY"], "127.0.0.1,localhost")
