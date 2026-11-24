"""Image-owned configuration for phase-local OpenCode 1.19.5."""

import json
import ipaddress
import os
import re
from urllib.parse import urlsplit


ROLES = {
    "intake",
    "specification",
    "build",
    "reviewer-code",
    "reviewer-security",
    "reviewer-ux",
    "reviewer-ui",
    "decision",
}


def relay_options():
    relay = os.environ.get("VIBEARR_OLLAMA_URL", "http://127.0.0.1:3130/v1")
    parsed = urlsplit(relay)
    try:
        address = ipaddress.ip_address(parsed.hostname or "")
        port = parsed.port
    except ValueError:
        raise ValueError("Use the registered numeric Ollama relay.") from None
    if (
        parsed.scheme != "http"
        or port != 3130
        or parsed.path != "/v1"
        or parsed.query
        or parsed.fragment
        or parsed.username
        or parsed.password
        or not address.is_private
        or address.is_link_local
        or address.is_unspecified
        or address.is_multicast
    ):
        raise ValueError("OpenCode relay differs from the registered worker transport.")
    grant = os.environ.get("VIBEARR_EGRESS_GRANT", "")
    if grant and not re.fullmatch(r"[A-Za-z0-9_-]{32,256}", grant):
        raise ValueError("OpenCode egress grant is invalid.")
    if not address.is_loopback and not grant:
        raise ValueError("Remote OpenCode relay requires its operation grant.")
    options = {"baseURL": relay}
    if grant:
        options["headers"] = {"X-Vibearr-Grant": grant}
    return options


def command(model, home, *, factory_role=None):
    component = r"[A-Za-z0-9][A-Za-z0-9_.-]*"
    pattern = component + r"(?:/" + component + r")*(?::" + component + r")?"
    if (
        not isinstance(model, str)
        or len(model) > 200
        or not re.fullmatch(pattern, model)
    ):
        raise ValueError("Select an installed external Ollama model.")
    role = os.environ.get("VIBEARR_FACTORY_ROLE")
    if role != factory_role:
        raise ValueError("OpenCode context differs from its registered factory role.")
    if role is not None and (not isinstance(role, str) or role not in ROLES):
        raise ValueError("Unsupported OpenCode factory role.")
    permissions = "allow"
    if role is not None and role != "build":
        permissions = {
            "*": "deny",
            "read": "allow",
            "glob": "allow",
            "grep": "allow",
            "list": "allow",
        }
    config = {
        "$schema": "https://opencode.ai/config.json",
        "provider": {
            "ollama": {
                "npm": "@ai-sdk/openai-compatible",
                "name": "External Ollama",
                "options": relay_options(),
                "models": {model: {"name": model}},
            }
        },
        "model": "ollama/" + model,
        "enabled_providers": ["ollama"],
        "plugin": [],
        "mcp": {},
        "permission": permissions,
        "autoupdate": False,
    }
    encoded = json.dumps(config)
    config_path = home / "opencode.json"
    descriptor = os.open(
        config_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
    )
    with os.fdopen(descriptor, "w") as stream:
        stream.write(encoded)
    for name in list(os.environ):
        if name.startswith("OPENCODE_"):
            del os.environ[name]
    os.environ.update(
        OPENCODE_CONFIG=str(config_path),
        OPENCODE_CONFIG_CONTENT=encoded,
        OPENCODE_CONFIG_DIR=str(home / ".config/opencode"),
        OPENCODE_DISABLE_PROJECT_CONFIG="true",
        OPENCODE_DISABLE_AUTOUPDATE="true",
        OPENCODE_DISABLE_DEFAULT_PLUGINS="true",
        OPENCODE_DISABLE_MODELS_FETCH="true",
        XDG_CONFIG_HOME=str(home / ".config"),
        XDG_DATA_HOME=str(home / ".local/share"),
        XDG_CACHE_HOME=str(home / ".cache"),
        XDG_STATE_HOME=str(home / ".local/state"),
        XDG_RUNTIME_DIR=str(home / ".runtime"),
    )
    (home / ".runtime").mkdir(mode=0o700, exist_ok=True)
    return [
        "/usr/local/bin/opencode",
        "run",
        "--pure",
        "--format",
        "json",
        "--model",
        "ollama/" + model,
    ]
