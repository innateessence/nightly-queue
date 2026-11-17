import json

from django.core.exceptions import ValidationError
from django.views.decorators.debug import sensitive_variables


def invalid_material():
    raise ValidationError(
        {
            "secret": "Enter valid JSON for the selected credential format; unknown fields are rejected."
        }
    )


def token_text(value):
    return (
        isinstance(value, str)
        and bool(value)
        and not any(character.isspace() for character in value)
    )


@sensitive_variables()
def validate_structured_secret(provider, kind, secret):
    if len(secret) > 65536:
        invalid_material()
    try:
        value = json.loads(secret)
    except (ValueError, RecursionError):
        invalid_material()
    if not isinstance(value, dict):
        invalid_material()
    if provider == "codex" and kind == "session":
        if set(value) - {"tokens", "auth_mode", "OPENAI_API_KEY", "last_refresh"}:
            invalid_material()
        if (
            value.get("auth_mode", "chatgpt") != "chatgpt"
            or value.get("OPENAI_API_KEY") is not None
        ):
            invalid_material()
        tokens = value.get("tokens")
        required = {
            "access_token",
            "refresh_token",
            "id_token",
            "account_id",
        }
        if (
            not isinstance(tokens, dict)
            or not required <= set(tokens)
            or set(tokens) - required - {"login_owner_id", "oauth_provenance"}
        ):
            invalid_material()
        if not all(token_text(tokens[key]) for key in required):
            invalid_material()
        if tokens.get("login_owner_id") is not None and not token_text(
            tokens["login_owner_id"]
        ):
            invalid_material()
        provenance = tokens.get("oauth_provenance")
        if provenance is not None and (
            not isinstance(provenance, dict)
            or set(provenance) != {"issuer", "client_id"}
            or not all(token_text(value) for value in provenance.values())
        ):
            invalid_material()
        if "last_refresh" in value and not isinstance(value["last_refresh"], str):
            invalid_material()
        return
    if provider != "opencode" or kind != "connection":
        invalid_material()
    if value.get("provider") == "ollama" and not value.get("api_key"):
        from vibearr_executor.ollama_relay import external_endpoint

        try:
            external_endpoint(value.get("base_url"))
            return
        except ValueError:
            pass
    from vibearr_runner.opencode_connection import validate_connection

    try:
        validate_connection(value)
    except ValueError:
        invalid_material()
