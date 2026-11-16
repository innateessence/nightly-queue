"""Offline pinned CLI checks; this never supplies a usable provider credential."""

import json
from pathlib import Path
import subprocess
import sys
import tempfile

from .opencode_task import VERSION, launch


def main():
    with tempfile.TemporaryDirectory() as temporary:
        context = {
            "schema_version": 1,
            "adapter": "opencode",
            "model": "openai/gpt-6.1-sol",
            "prompt": "Offline configuration inspection only.",
            "auth_kind": "connection",
            "account_digest": "a" * 64,
        }
        secret = json.dumps(
            {"provider": "openai", "api_key": "fixture-only-key-0123456789"}
        )
        arguments, environment, _ = launch(
            context, secret, home=Path(temporary) / "native"
        )
        help_result = subprocess.run(
            [arguments[0], "run", "--help"],
            env=environment,
            cwd=Path(temporary) / "native",
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
        for flag in (
            "--format",
            "--auto",
            "--model",
            "--agent",
            "--session",
            "--attach",
        ):
            assert flag in help_result.stdout + help_result.stderr, flag
        result = subprocess.run(
            [arguments[0], "debug", "config"],
            env=environment,
            cwd=Path(temporary) / "native",
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
        config = json.loads(result.stdout)
        assert config["model"] == context["model"]
        assert config["small_model"] == context["model"]
        assert config["enabled_providers"] == ["openai"]
        assert config["mcp"] == {} and config["plugin"] == []
        assert config["permission"]["*"] == "deny"
        assert config["permission"]["bash"] == "allow"
        assert config["lsp"] is False and config["formatter"] is False
        context_path = Path(temporary) / "context.json"
        context_path.write_text(json.dumps(context))
        empty_secret = Path(temporary) / "empty-secret"
        empty_secret.write_text("")
        missing_environment = dict(environment)
        missing_environment["VIBEARR_CONTEXT"] = str(context_path)
        missing_environment["VIBEARR_BACKEND_SECRET_FILE"] = str(empty_secret)
        missing = subprocess.run(
            [sys.executable, "-I", "-m", "vibearr_executor.opencode_task"],
            env=missing_environment,
            cwd=Path(temporary) / "native",
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert missing.returncode == 78
        assert json.loads(missing.stdout)["type"] == "error"
        assert "fixture-only-key" not in missing.stdout + missing.stderr
        print(
            json.dumps(
                {
                    "pinned_version": VERSION,
                    "offline_cli_configuration": True,
                    "missing_auth_rejected_before_cli": True,
                    "authenticated": False,
                    "native_task_acceptance": False,
                }
            )
        )


if __name__ == "__main__":
    main()
