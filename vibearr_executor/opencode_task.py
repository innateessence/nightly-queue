"""Fixed OpenCode recipe; repository files cannot configure the native process."""

import json
import os
from pathlib import Path
import re
import subprocess

from .codex_task import DATABASE_ENV, disposable_database_environment

VERSION = "1.19.5"
BINARY = "/usr/local/bin/opencode"
SECRET_ENV = "VIBEARR_OPENCODE_AUTH"
ARGV = ("/usr/local/bin/python", "-I", "-m", "vibearr_executor.opencode_task")
HOSTS = ("api.openai.com",)
ENDPOINT = "https://api.openai.com/v1"


def model_id(value):
    return (
        isinstance(value, str)
        and re.fullmatch(r"openai/[A-Za-z0-9][A-Za-z0-9._:-]{0,191}", value) is not None
    )


def auth_material(kind, secret):
    try:
        if (
            kind != "connection"
            or not isinstance(secret, str)
            or len(secret.encode()) > 16384
        ):
            raise ValueError
        value = json.loads(secret)
        if not isinstance(value, dict) or set(value) - {
            "provider",
            "api_key",
            "base_url",
        }:
            raise ValueError
        key = value["api_key"]
        if (
            value.get("provider") != "openai"
            or value.get("base_url", ENDPOINT) != ENDPOINT
        ):
            raise ValueError
        if (
            not isinstance(key, str)
            or not 16 <= len(key) <= 8192
            or any(c.isspace() for c in key)
            or "\x00" in key
        ):
            raise ValueError
        return json.dumps(
            {"provider": "openai", "api_key": key, "base_url": ENDPOINT},
            sort_keys=True,
            separators=(",", ":"),
        )
    except (ValueError, KeyError, TypeError, UnicodeError, RecursionError):
        raise ValueError(
            "Select an OpenCode OpenAI API-key connection with its fixed official endpoint."
        ) from None


def context_data(value):
    fields = {
        "schema_version",
        "adapter",
        "model",
        "prompt",
        "auth_kind",
        "account_digest",
    }
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError("Invalid OpenCode task context.")
    if (
        type(value["schema_version"]) is not int
        or value["schema_version"] != 1
        or value["adapter"] != "opencode"
    ):
        raise ValueError("Unsupported OpenCode task context.")
    if value["auth_kind"] != "connection" or not model_id(value["model"]):
        raise ValueError("Select an explicit openai/model and OpenCode connection.")
    if (
        not isinstance(value["account_digest"], str)
        or re.fullmatch(r"[a-f0-9]{64}", value["account_digest"]) is None
    ):
        raise ValueError("Invalid OpenCode account binding.")
    if (
        not isinstance(value["prompt"], str)
        or not value["prompt"].strip()
        or "\x00" in value["prompt"]
    ):
        raise ValueError("Supply a nonempty OpenCode task prompt.")
    return value


def configuration(model):
    permissions = {
        "*": "deny",
        "read": "allow",
        "edit": "allow",
        "glob": "allow",
        "grep": "allow",
        "list": "allow",
        "bash": "allow",
        "todowrite": "allow",
    }
    return {
        "model": model,
        "small_model": model,
        "enabled_providers": ["openai"],
        "provider": {
            "openai": {
                "options": {"baseURL": ENDPOINT},
                "whitelist": [model.split("/", 1)[1]],
            }
        },
        "default_agent": "build",
        "agent": {
            "build": {"permission": permissions},
            "title": {"disable": True},
            "summary": {"disable": True},
        },
        "permission": permissions,
        "plugin": [],
        "mcp": {},
        "instructions": [],
        "skills": {"paths": [], "urls": []},
        "formatter": False,
        "lsp": False,
        "autoupdate": False,
        "share": "disabled",
        "snapshot": False,
    }


def launch(context, secret, *, home, binary=BINARY):
    context = context_data(context)
    connection = json.loads(auth_material(context["auth_kind"], secret))
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
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
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL="/dev/null",
        NO_PROXY="127.0.0.1,localhost",
        no_proxy="127.0.0.1,localhost",
        PYTHONDONTWRITEBYTECODE="1",
        PYTHONUNBUFFERED="1",
        TMPDIR=str(home / "tmp"),
    )
    for variable, directory in {
        "XDG_CONFIG_HOME": "config",
        "XDG_DATA_HOME": "data",
        "XDG_STATE_HOME": "state",
        "XDG_CACHE_HOME": "cache",
        "TMPDIR": "tmp",
    }.items():
        path = home / directory
        path.mkdir(mode=0o700)
        environment[variable] = str(path)
    environment.update(
        OPENCODE_DISABLE_PROJECT_CONFIG="1",
        OPENCODE_PURE="1",
        OPENCODE_DISABLE_DEFAULT_PLUGINS="1",
        OPENCODE_DISABLE_EXTERNAL_SKILLS="1",
        OPENCODE_DISABLE_CLAUDE_CODE="1",
        OPENCODE_DISABLE_LSP_DOWNLOAD="1",
        OPENCODE_DISABLE_AUTOUPDATE="1",
        OPENCODE_DISABLE_MODELS_FETCH="1",
        OPENCODE_EXPERIMENTAL="0",
        OPENCODE_DISABLE_FFF="1",
        OPENCODE_EXPERIMENTAL_DISABLE_FILEWATCHER="1",
        OPENCODE_CONFIG_CONTENT=json.dumps(
            configuration(context["model"]), separators=(",", ":")
        ),
        OPENCODE_AUTH_CONTENT=json.dumps(
            {"openai": {"type": "api", "key": connection["api_key"]}},
            separators=(",", ":"),
        ),
        OPENCODE_DB=str(home / "data" / "opencode.db"),
        OPENCODE_TEST_HOME=str(home),
        OPENCODE_TEST_MANAGED_CONFIG_DIR=str(home / "managed"),
        OPENCODE_CONFIG=str(home / "config" / "opencode.json"),
        OPENCODE_CONFIG_DIR=str(home / "config"),
    )
    if os.environ.get(DATABASE_ENV):
        environment.update(disposable_database_environment(os.environ[DATABASE_ENV]))
    version = subprocess.run(
        [binary, "--version"],
        env=environment,
        cwd=home,
        capture_output=True,
        timeout=10,
    )
    if version.returncode or version.stdout.strip() != VERSION.encode():
        raise ValueError("Installed OpenCode CLI does not match the pinned version.")
    prompt = home / "prompt.txt"
    prompt.write_text(context["prompt"])
    prompt.chmod(0o600)
    arguments = [
        binary,
        "run",
        "--format",
        "json",
        "--auto",
        "--model",
        context["model"],
        "--agent",
        "build",
        "--dir",
        "/work",
    ]
    return arguments, environment, prompt


def main():
    try:
        path = Path(os.environ["VIBEARR_CONTEXT"])
        if path.stat().st_size > 1024 * 1024:
            raise ValueError("OpenCode context exceeds the byte limit.")
        context = json.loads(path.read_bytes())
        secret = Path(os.environ["VIBEARR_BACKEND_SECRET_FILE"]).read_text()
        arguments, environment, prompt = launch(
            context, secret, home=Path("/home/task/native-opencode")
        )
        with prompt.open("rb") as stream:
            os.dup2(stream.fileno(), 0)
        os.chdir(prompt.parent)
        os.execve(BINARY, arguments, environment)
    except (ValueError, KeyError, OSError, subprocess.TimeoutExpired):
        print(
            json.dumps(
                {
                    "type": "error",
                    "error": "Native OpenCode configuration/authentication is unavailable; inspect selected connection, model, pinned image and context.",
                }
            ),
            flush=True,
        )
        raise SystemExit(78) from None


if __name__ == "__main__":
    main()
