"""Strict operator-managed provider connections; no agent-selected destinations."""

import ipaddress
import re
from typing import Any
from urllib.parse import urlsplit

OFFICIAL_ENDPOINTS = {
    "openai": "https://api.openai.com/v1",
    "anthropic": "https://api.anthropic.com/v1",
    "opencode": "https://opencode.ai/inference/openai/v1",
}
LOCAL_HOSTS = {"localhost", "runner-ollama", "host.docker.internal"}


def valid_model(value: Any) -> bool:
    component = r"[A-Za-z0-9][A-Za-z0-9_.-]*"
    pattern = component + r"(?:/" + component + r")*(?::" + component + r")?"
    return (
        isinstance(value, str)
        and len(value) <= 200
        and re.fullmatch(pattern, value) is not None
    )


def validate_connection(value: Any) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) - {"provider", "api_key", "base_url"}:
        raise ValueError("Unsupported connection fields.")
    provider = value.get("provider")
    if not isinstance(provider, str) or provider not in {*OFFICIAL_ENDPOINTS, "ollama"}:
        raise ValueError("Select a supported inference provider.")
    key = value.get("api_key", "")
    if (
        not isinstance(key, str)
        or len(key) > 65536
        or any(char.isspace() for char in key)
    ):
        raise ValueError("Enter a valid API key.")
    if provider != "ollama" and not key:
        raise ValueError("This provider requires an API key.")
    endpoint = value.get("base_url", OFFICIAL_ENDPOINTS.get(provider))
    if not isinstance(endpoint, str) or any(char.isspace() for char in endpoint):
        raise ValueError("Enter the inference endpoint.")
    try:
        parsed = urlsplit(endpoint)
        valid = parsed.scheme in {"http", "https"} and bool(parsed.hostname)
        valid = valid and not (
            parsed.username or parsed.password or parsed.query or parsed.fragment
        )
        valid = valid and parsed.port != 0
    except ValueError:
        raise ValueError("Enter a valid HTTP(S) inference endpoint.") from None
    if not valid:
        raise ValueError(
            "URLs cannot contain credentials, query parameters or fragments."
        )
    endpoint = endpoint.rstrip("/")
    if provider != "ollama":
        if endpoint != OFFICIAL_ENDPOINTS[provider]:
            raise ValueError("Hosted providers require their fixed official endpoint.")
        return {"provider": provider, "api_key": key, "base_url": endpoint}
    if parsed.path not in {"", "/", "/api", "/api/"}:
        raise ValueError("Use the Ollama origin or its /api base path.")
    hostname = parsed.hostname
    if hostname not in LOCAL_HOSTS:
        try:
            address = ipaddress.ip_address(hostname or "")
        except ValueError:
            raise ValueError(
                "Select an explicit trusted local Ollama host or private IP address."
            ) from None
        if (
            not (address.is_private or address.is_loopback)
            or address.is_link_local
            or address.is_unspecified
            or address.is_multicast
        ):
            raise ValueError("Select a trusted local Ollama endpoint.")
    origin = parsed.scheme + "://" + parsed.netloc
    return {"provider": provider, "api_key": key, "base_url": origin + "/api"}
