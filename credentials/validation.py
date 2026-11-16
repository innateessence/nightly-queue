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
        if not isinstance(tokens, dict) or set(tokens) != {
            "access_token",
            "refresh_token",
            "id_token",
            "account_id",
        }:
            invalid_material()
        if not all(token_text(entry) for entry in tokens.values()):
            invalid_material()
        if "last_refresh" in value and not isinstance(value["last_refresh"], str):
            invalid_material()
        return
    if provider != "opencode" or kind != "connection":
        invalid_material()
    from coding_runner.opencode_connection import validate_connection

    try:
        validate_connection(value)
    except ValueError:
        invalid_material()
