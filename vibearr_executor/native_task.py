"""Invoke one pinned CLI with image-owned configuration and no API-key fallback."""

import json
import os
from pathlib import Path
import subprocess
import sys

from .integrated_profiles import VERSIONS
from .managed_login import install_login


def command(provider, model, home, *, factory_role=None):
    if provider == "codex":
        directory = home / ".codex"
        os.environ["CODEX_HOME"] = str(directory)
        directory.mkdir(mode=0o700, exist_ok=True)
        (directory / "config.toml").write_text(
            'forced_login_method = "chatgpt"\ncli_auth_credentials_store = "file"\nweb_search = "disabled"\n'
        )
        arguments = [
            "/usr/local/bin/codex",
            "exec",
            "--json",
            "--model",
            model,
            "--skip-git-repo-check",
            "--ignore-user-config",
            "--ignore-rules",
            "--strict-config",
            "--dangerously-bypass-approvals-and-sandbox",
            "-c",
            'projects={"/work"={trust_level="untrusted"}}',
        ]
        overrides = {
            "forced_login_method": "chatgpt",
            "cli_auth_credentials_store": "file",
            "mcp_servers": {},
            "plugins._default.enabled": False,
            "cloud.skills.enabled": False,
            "skills.include_instructions": False,
            "web_search": "disabled",
        }
        for key, value in overrides.items():
            arguments.extend(
                ["-c", key + "=" + json.dumps(value, separators=(",", ":"))]
            )
        for feature in (
            "plugins",
            "hooks",
            "plugin_hooks",
            "apps",
            "skill_mcp_dependency_install",
        ):
            arguments.extend(["--disable", feature])
        arguments.extend(["--enable", "code_mode_host", "-"])
        return arguments
    if provider == "claude":
        from .claude_task import command as claude_command

        return claude_command(model, home, factory_role=factory_role)
    if provider != "opencode":
        raise ValueError("Unsupported coding agent.")
    from .opencode_task import command as opencode_command

    return opencode_command(model, home, factory_role=factory_role)


def main():
    context = json.loads(Path(os.environ["VIBEARR_CONTEXT"]).read_bytes())
    provider, model = context["adapter"], context["model"]
    if (
        provider not in VERSIONS
        or not isinstance(model, str)
        or not model
        or len(model) > 200
    ):
        raise ValueError("Unsupported native agent or model.")
    connection = json.loads(
        Path(os.environ["VIBEARR_BACKEND_SECRET_FILE"]).read_bytes()
    )
    if connection.get("schema_version") == 1 and "scope" in connection:
        connection = connection["connection"]
    if connection["provider"] != provider:
        raise ValueError("Selected connection differs from the task provider.")
    if any(
        name in os.environ
        for name in (
            "OPENAI_API_KEY",
            "CODEX_API_KEY",
            "ANTHROPIC_API_KEY",
            "OPENROUTER_API_KEY",
        )
    ):
        raise ValueError(
            "Model API keys are forbidden in subscription/local execution."
        )
    home = Path(os.environ["HOME"])
    if provider != "opencode":
        install_login(provider, connection["files"], home)
    from urllib.parse import urlsplit

    bypass = "127.0.0.1,localhost"
    if os.environ.get("VIBEARR_OLLAMA_URL"):
        bypass += "," + (urlsplit(os.environ["VIBEARR_OLLAMA_URL"]).hostname or "")
    os.environ.update(
        NO_PROXY=bypass,
        no_proxy=bypass,
        XDG_CONFIG_HOME=str(home / ".config"),
        XDG_DATA_HOME=str(home / ".local/share"),
        XDG_CACHE_HOME=str(home / ".cache"),
    )
    if os.environ.get("DATABASE_URL"):
        from urllib.parse import urlsplit

        database = urlsplit(os.environ["DATABASE_URL"])
        os.environ.update(
            POSTGRES_HOST=database.hostname or "",
            POSTGRES_PORT=str(database.port or 5432),
            VIBEARR_POSTGRES_DB="task",
            VIBEARR_POSTGRES_USER="task",
            VIBEARR_POSTGRES_PASSWORD=database.password or "",
        )
    arguments = command(provider, model, home, factory_role=context.get("factory_role"))
    process = subprocess.Popen(arguments, stdin=subprocess.PIPE)
    assert process.stdin is not None
    process.stdin.write(context["prompt"].encode())
    process.stdin.close()
    return process.wait()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError):
        print(
            '{"type":"error","error":"Selected native login, model or connection is unavailable."}',
            file=sys.stderr,
        )
        raise SystemExit(1)
