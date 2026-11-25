"""Shared metadata digests, repository validation and recovery deadlines."""

import hashlib
import json
import math
import re
import time
from typing import Any, Callable
from urllib.parse import urlsplit


class ContextError(ValueError):
    pass


class ContextInterrupted(ContextError):
    pass


def check_deadline(
    deadline: float | None, checkpoint: Callable[[], bool] | None
) -> float | None:
    if checkpoint is not None and checkpoint() is not True:
        raise ContextInterrupted(
            "Discussion refresh interrupted by execution controls."
        )
    if deadline is None:
        return None
    remaining = deadline - time.monotonic()
    if not math.isfinite(remaining) or remaining <= 0:
        raise ContextError("Discussion refresh exhausted its active budget.")
    return remaining


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def text(value: Any, name: str, *, empty: bool = False) -> str:
    if not isinstance(value, str) or (not empty and not value.strip()):
        raise ContextError(f"Invalid {name}.")
    return value


def repository_path(repository: Any) -> str:
    parsed = urlsplit(text(repository, "issue repository"))
    if (
        parsed.scheme != "https"
        or parsed.netloc != "github.com"
        or parsed.query
        or parsed.fragment
    ):
        raise ContextError("Expected a GitHub issue repository URL.")
    path = parsed.path.rstrip("/").removesuffix(".git")
    if not re.fullmatch(r"/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", path):
        raise ContextError("Invalid issue repository.")
    return path
