import json

from django.core.exceptions import ValidationError
from django.views.decorators.debug import sensitive_variables

from vibearr_executor.ollama_relay import external_endpoint


@sensitive_variables()
def validate_structured_secret(provider, kind, secret):
    try:
        if len(secret) > 65536 or provider != "opencode" or kind != "connection":
            raise ValueError
        value = json.loads(secret)
        if not isinstance(value, dict) or set(value) != {"provider", "base_url"}:
            raise ValueError
        if value["provider"] != "ollama":
            raise ValueError
        external_endpoint(value["base_url"])
    except (ValueError, TypeError, RecursionError):
        raise ValidationError(
            {
                "secret": "Enter an external Ollama connection with only provider and base_url."
            }
        ) from None
