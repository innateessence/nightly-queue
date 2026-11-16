"""Fixed Codex recipe; candidate files cannot supply launch configuration."""

import base64
from datetime import datetime, timezone
import json
import ipaddress
import os
from pathlib import Path
import re
import subprocess
from urllib.parse import urlsplit

VERSION = "0.169.0"
BINARY = "/usr/local/bin/codex"
SECRET_ENV = "VIBEARR_CODEX_AUTH"
ARGV = ("/usr/local/bin/python", "-I", "-m", "vibearr_executor.codex_task")
HOSTS = {"token": ("api.openai.com",), "session": ("chatgpt.com",)}
DATABASE_ENV = "VIBEARR_DISPOSABLE_DATABASE_URL"


def disposable_database_environment(url: str) -> dict:
    try:
        connection = urlsplit(url)
        address = ipaddress.IPv4Address(connection.hostname or "")
        scoped = (
            connection.scheme == "postgresql"
            and connection.username == "task"
            and connection.path == "/task"
            and connection.port == 5432
            and not connection.query
            and not connection.fragment
            and re.fullmatch(r"[a-f0-9]{48}", connection.password or "") is not None
            and address.is_private
            and not address.is_loopback
            and not address.is_link_local
            and not address.is_unspecified
            and not address.is_reserved
        )
        if not scoped:
            raise ValueError
    except (ValueError, TypeError):
        raise ValueError(
            "Executor disposable database connection is invalid."
        ) from None
    fields = {
        "POSTGRES_DB": "task",
        "POSTGRES_USER": "task",
        "POSTGRES_PASSWORD": connection.password,
        "POSTGRES_HOST": str(address),
        "POSTGRES_PORT": "5432",
    }
    return {
        "DATABASE_URL": url,
        **fields,
        **{"VIBEARR_" + key: value for key, value in fields.items()},
    }


def model_id(value):
    return (
        isinstance(value, str)
        and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,199}", value) is not None
    )


def auth_material(kind: str, secret: str, *, runtime=False) -> str:
    if (
        not isinstance(secret, str)
        or not 16 <= len(secret.encode()) <= 16384
        or "\x00" in secret
    ):
        raise ValueError("Selected Codex credential is unavailable or invalid.")
    if kind == "token":
        if any(character.isspace() for character in secret) or secret.startswith("{"):
            raise ValueError("Select a dedicated Codex API key.")
        return secret
    if kind != "session":
        raise ValueError("Selected Codex credential kind is unsupported.")
    try:
        value = json.loads(secret)
        if not isinstance(value, dict) or set(value) - {
            "auth_mode",
            "OPENAI_API_KEY",
            "tokens",
            "last_refresh",
        }:
            raise ValueError
        tokens = value["tokens"]
        expected_mode = "chatgptAuthTokens" if runtime else "chatgpt"
        if (
            value.get("auth_mode", "chatgpt") != expected_mode
            or value.get("OPENAI_API_KEY") is not None
        ):
            raise ValueError
        fields = {
            "access_token",
            "refresh_token",
            "id_token",
            "account_id",
        }
        optional = set() if runtime else {"login_owner_id", "oauth_provenance"}
        if (
            not isinstance(tokens, dict)
            or not fields <= set(tokens)
            or set(tokens) - fields - optional
        ):
            raise ValueError
        tokens = {key: tokens[key] for key in fields}
        required = {
            key: token
            for key, token in tokens.items()
            if key != "refresh_token" or not runtime
        }
        if not all(
            isinstance(token, str) and token and not any(c.isspace() for c in token)
            for token in required.values()
        ):
            raise ValueError
        if runtime and tokens["refresh_token"] != "":
            raise ValueError
        access = tokens["access_token"]
        claims = json.loads(base64.urlsafe_b64decode(access.split(".")[1] + "==="))
        if not isinstance(claims, dict):
            raise ValueError
        if (
            type(claims.get("exp")) is not int
            or claims["exp"] <= datetime.now(timezone.utc).timestamp() + 300
        ):
            raise ValueError
        return json.dumps(
            {
                "auth_mode": "chatgptAuthTokens",
                "OPENAI_API_KEY": None,
                "tokens": {**tokens, "refresh_token": ""},
                "last_refresh": "1970-01-01T00:00:00Z",
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    except (ValueError, TypeError, KeyError, IndexError, UnicodeError, RecursionError):
        raise ValueError(
            "Selected Codex session is invalid or expires soon; replace it outside the task."
        ) from None


def context_data(value):
    if not isinstance(value, dict) or set(value) != {
        "schema_version",
        "adapter",
        "model",
        "prompt",
        "auth_kind",
        "account_digest",
    }:
        raise ValueError("Invalid Codex task context.")
    if (
        type(value["schema_version"]) is not int
        or value["schema_version"] != 1
        or value["adapter"] != "codex"
    ):
        raise ValueError("Unsupported Codex task context.")
    if (
        not model_id(value["model"])
        or not isinstance(value["auth_kind"], str)
        or value["auth_kind"] not in HOSTS
    ):
        raise ValueError("Select an explicit Codex model and supported authentication.")
    if (
        not isinstance(value["account_digest"], str)
        or re.fullmatch(r"[a-f0-9]{64}", value["account_digest"]) is None
    ):
        raise ValueError("Native Codex account binding is invalid.")
    if (
        not isinstance(value["prompt"], str)
        or not value["prompt"].strip()
        or "\x00" in value["prompt"]
    ):
        raise ValueError("Supply a nonempty Codex task prompt.")
    return value


def launch(
    context: dict, secret: str, *, home: Path, binary: str = BINARY
) -> tuple[list[str], dict, Path]:
    context = context_data(context)
    material = auth_material(context["auth_kind"], secret, runtime=True)
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    codex_home = home / ".codex"
    codex_home.mkdir(mode=0o700)
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
        CODEX_HOME=str(codex_home),
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
    if context["auth_kind"] == "token":
        environment["CODEX_API_KEY"] = material
    else:
        path = codex_home / "auth.json"
        path.write_text(material)
        path.chmod(0o600)
    version = subprocess.run(
        [binary, "--version"], env=environment, capture_output=True, timeout=5
    )
    if (
        version.returncode
        or version.stdout.strip() != ("codex-cli " + VERSION).encode()
    ):
        raise ValueError("Installed Codex CLI does not match the pinned version.")
    arguments = [
        binary,
        "exec",
        "--json",
        "--color",
        "never",
        "--ignore-user-config",
        "--ignore-rules",
        "--strict-config",
        "--dangerously-bypass-approvals-and-sandbox",
        "--model",
        context["model"],
    ]
    overrides = {
        "projects": {"/work": {"trust_level": "untrusted"}},
        "mcp_servers": {},
        "plugins._default.enabled": False,
        "cloud.skills.enabled": False,
        "skills.include_instructions": False,
        "web_search": "disabled",
        "cli_auth_credentials_store": "file",
    }
    for key, value in overrides.items():
        if key == "projects":
            literal = '{"/work"={trust_level="untrusted"}}'
        else:
            literal = json.dumps(value, separators=(",", ":"))
        arguments.extend(("-c", key + "=" + literal))
    for feature in (
        "plugins",
        "hooks",
        "plugin_hooks",
        "apps",
        "skill_mcp_dependency_install",
    ):
        arguments.extend(("--disable", feature))
    arguments.extend(("--enable", "code_mode_host"))
    prompt = home / "prompt.txt"
    prompt.write_text(context["prompt"])
    prompt.chmod(0o600)
    arguments.append("-")
    return arguments, environment, prompt


def main():
    try:
        path = Path(os.environ["VIBEARR_CONTEXT"])
        if path.stat().st_size > 1024 * 1024:
            raise ValueError("Codex context exceeds the byte limit.")
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
                    "message": "Native Codex configuration/authentication is unavailable; inspect selected account, pinned image and context.",
                }
            ),
            flush=True,
        )
        raise SystemExit(78) from None


if __name__ == "__main__":
    main()
