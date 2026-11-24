import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from vibearr_executor.opencode_task import ROLES, command


BINARY = Path("/usr/local/bin/opencode")


@unittest.skipUnless(BINARY.is_file(), "Build the pinned OpenCode probe image")
class OpenCodeExecutableTests(unittest.TestCase):
    def test_pinned_cli_exposes_registered_noninteractive_arguments(self):
        version = subprocess.check_output([BINARY, "--version"], text=True, timeout=20)
        self.assertEqual(version.strip(), "1.19.5")
        result = subprocess.run(
            [BINARY, "run", "--help"],
            text=True,
            capture_output=True,
            timeout=20,
            check=True,
        )
        help_text = result.stdout + result.stderr
        for flag in ("--pure", "--format", "--model"):
            self.assertIn(flag, help_text)

    def test_pinned_cli_ignores_candidate_configuration_for_every_role(self):
        for role in sorted(ROLES):
            with self.subTest(role=role), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                home = root / "home"
                home.mkdir(mode=0o700)
                candidate = root / "candidate"
                candidate.mkdir()
                (candidate / "opencode.json").write_text(
                    json.dumps(
                        {
                            "model": "openai/untrusted",
                            "plugin": ["file:///not-permitted"],
                            "permission": "allow",
                        }
                    )
                )
                (candidate / "opencode.jsonc").write_text('{"model":"openai/other"}')
                (candidate / ".opencode").mkdir()
                (candidate / ".opencode/opencode.json").write_text(
                    '{"permission":"allow"}'
                )
                with patch.dict(
                    os.environ,
                    {
                        "HOME": str(home),
                        "VIBEARR_FACTORY_ROLE": role,
                        "OPENCODE_PERMISSION": '"allow"',
                        "OPENCODE_DB": "/control/untrusted.sqlite",
                    },
                    clear=True,
                ):
                    command("qwen3-coder:30b", home, factory_role=role)
                    result = subprocess.run(
                        [BINARY, "debug", "config", "--pure"],
                        cwd=candidate,
                        text=True,
                        capture_output=True,
                        timeout=30,
                        check=True,
                    )
                config = json.loads(result.stdout)
                self.assertEqual(config["model"], "ollama/qwen3-coder:30b")
                self.assertEqual(config["enabled_providers"], ["ollama"])
                self.assertFalse(config.get("plugin"))
                self.assertFalse(config.get("mcp"))
                expected = "allow" if role == "build" else "deny"
                self.assertEqual(config["permission"]["*"], expected)
                if role != "build":
                    for tool in ("read", "glob", "grep", "list"):
                        self.assertEqual(config["permission"][tool], "allow")
