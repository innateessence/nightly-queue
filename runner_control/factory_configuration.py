"""Validate factory settings within the selected native connection."""

from django.core.exceptions import ValidationError

from vibearr_runner.factory_routing import (
    AGENT_ROLES,
    FactoryRoutingError,
    default_configuration,
    normalize_configuration,
)
from vibearr_runner.policy import ExecutionPolicy

from .validators import validate_backend_model


def validate_factory_configuration(value):
    try:
        settings = normalize_configuration(value)
    except FactoryRoutingError as exc:
        raise ValidationError(str(exc)) from None
    for role in settings["roles"].values():
        model = role.get("model", "")
        if model and (
            any(character.isspace() for character in model)
            or "://" in model
            or model.startswith("-")
        ):
            raise ValidationError(
                "Factory models must be model IDs, not URLs or CLI arguments."
            )


def effective_factory_configuration(value, effective):
    validate_factory_configuration(value)
    settings = normalize_configuration(value)
    backend = effective["implementation_backend"]
    if settings["enabled"] and backend not in {"codex", "claude", "opencode"}:
        raise ValidationError("Select a native backend before enabling factory roles.")
    roles = {}
    for role in AGENT_ROLES:
        override = settings["roles"].get(role, {})
        model = override.get("model") or effective["implementation_model"]
        instructions = (
            override.get("instructions") or effective["implementation_instructions"]
        )
        if settings["enabled"] and not model:
            raise ValidationError(f"Select a model for factory role {role}.")
        if settings["enabled"] or override.get("model"):
            validate_backend_model(backend, model)
            if backend == "codex" and model.startswith("claude-"):
                raise ValidationError(
                    f"Factory role {role} must use the selected Codex provider."
                )
        roles[role] = {"model": model, "instructions": instructions}
    settings["roles"] = roles
    settings["max_repairs"] = min(
        settings["max_repairs"],
        ExecutionPolicy.from_data(effective["execution_policy"]).repair_limit,
    )
    return settings


def factory_defaults():
    return default_configuration()
