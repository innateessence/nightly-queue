"""Validate selected Ollama model names without commands or endpoints."""

import re
from typing import Any


def valid_model(value: Any) -> bool:
    component = r"[A-Za-z0-9][A-Za-z0-9_.-]*"
    pattern = component + r"(?:/" + component + r")*(?::" + component + r")?"
    return (
        isinstance(value, str)
        and len(value) <= 200
        and re.fullmatch(pattern, value) is not None
    )
