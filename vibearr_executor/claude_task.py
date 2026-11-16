"""Fixed Claude Code recipe inside the registered OCI boundary."""

import json
import os
from pathlib import Path
import re
import subprocess

from .codex_task import DATABASE_ENV, disposable_database_environment

VERSION = "2.1.325"
BINARY = "/usr/local/bin/claude"
SECRET_ENV = "VIBEARR_CLAUDE_AUTH"
ARGV = ("/usr/local/bin/python", "-I", "-m", "vibearr_executor.claude_task")
HOSTS = {"token": ("api.anthropic.com",)}
TOOLS = ("Bash", "Read", "Edit", "Write", "Glob", "Grep")


def auth_material(kind: str, secret: str, *, runtime=False) -> str:
    if (
        kind != "token"
        or not isinstance(secret, str)
        or not 16 <= len(secret.encode()) <= 16384
        or any(character.isspace() for character in secret)
        or "\x00" in secret
        or secret.startswith(("{", "sk-ant-oat"))
    ):
        raise ValueError(
            "Select a dedicated Claude API key; subscription login is unsupported."
        )
    return secret


def context_data(value):
    expected = {
        "schema_version",
        "adapter",
        "model",
        "prompt",
        "auth_kind",
        "account_digest",
    }
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError("Invalid Claude task context.")
    if (
        type(value["schema_version"]) is not int
        or value["schema_version"] != 1
        or value["adapter"] != "claude"
    ):
        raise ValueError("Unsupported Claude task context.")
    if (
        not isinstance(value["model"], str)
        or re.fullmatch(r"claude-[a-z0-9]+(?:-[a-z0-9]+)*", value["model"]) is None
    ):
        raise ValueError("Select an explicit Claude API model ID.")
    if len(value["model"]) > 200 or value["auth_kind"] != "token":
        raise ValueError(
            "Select an explicit Claude API model and API-key authentication."
        )
    if (
        not isinstance(value["account_digest"], str)
        or re.fullmatch(r"[a-f0-9]{64}", value["account_digest"]) is None
    ):
        raise ValueError("Native Claude account binding is invalid.")
    if (
        not isinstance(value["prompt"], str)
        or not value["prompt"].strip()
        or "\x00" in value["prompt"]
    ):
        raise ValueError("Supply a nonempty Claude task prompt.")
    return value


def launch(
    context: dict, secret: str, *, home: Path, binary: str = BINARY
) -> tuple[list[str], dict, Path]:
    context = context_data(context)
    material = auth_material(context["auth_kind"], secret, runtime=True)
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    configuration = home / ".claude"
    configuration.mkdir(mode=0o700)
    environment = {
        key: os.environ[key]
        for key in (
            "PATH",
            "LANG",
            "HTTPS_PROXY",
            "HTTP_PROXY",
            "https_proxy",
            "http_proxy",
        )
        if key in os.environ
    }
    environment.update(
        HOME=str(home),
        CLAUDE_CONFIG_DIR=str(configuration),
        ANTHROPIC_API_KEY=material,
        DISABLE_UPDATES="1",
        DISABLE_AUTOUPDATER="1",
        CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1",
        ENABLE_CLAUDEAI_MCP_SERVERS="false",
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL="/dev/null",
        NO_PROXY="127.0.0.1,localhost",
        no_proxy="127.0.0.1,localhost",
        PYTHONDONTWRITEBYTECODE="1",
        PYTHONUNBUFFERED="1",
        TMPDIR="/work",
    )
    if os.environ.get(DATABASE_ENV):
        environment.update(disposable_database_environment(os.environ[DATABASE_ENV]))
    version = subprocess.run(
        [binary, "--version"], env=environment, capture_output=True, timeout=5
    )
    if (
        version.returncode
        or version.stdout.strip() != (VERSION + " (Claude Code)").encode()
    ):
        raise ValueError("Installed Claude CLI does not match the pinned version.")
    arguments = [
        binary,
        "--bare",
        "--safe-mode",
        "--disable-slash-commands",
        "--print",
        "--output-format",
        "stream-json",
        "--verbose",
        "--model",
        context["model"],
        "--tools",
        ",".join(TOOLS),
        "--allowedTools",
        ",".join(TOOLS),
        "--permission-mode",
        "dontAsk",
        "--permission-prompts",
        "none",
        "--setting-sources",
        "",
        "--strict-mcp-config",
        "--mcp-config",
        '{"mcpServers":{}}',
        "--no-session-persistence",
    ]
    prompt = home / "prompt.txt"
    prompt.write_text(context["prompt"])
    prompt.chmod(0o600)
    return arguments, environment, prompt


def main():
    try:
        path = Path(os.environ["VIBEARR_CONTEXT"])
        if path.stat().st_size > 1024 * 1024:
            raise ValueError("Claude context exceeds the byte limit.")
        context = json.loads(path.read_bytes())
        secret = Path(os.environ["VIBEARR_BACKEND_SECRET_FILE"]).read_text()
        arguments, environment, prompt = launch(
            context, secret, home=Path("/home/task")
        )
        with prompt.open("rb") as stream:
            os.dup2(stream.fileno(), 0)
        os.execve(BINARY, arguments, environment)
    except (ValueError, KeyError, OSError, subprocess.TimeoutExpired):
        print(
            json.dumps(
                {
                    "type": "error",
                    "message": "Native Claude configuration/authentication is unavailable; inspect selected API key, pinned image and context.",
                }
            ),
            flush=True,
        )
        raise SystemExit(78) from None


if __name__ == "__main__":
    main()
