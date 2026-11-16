"""Environment names shared by the app and standalone clients."""

import os
import warnings


def canonical_name(legacy):
    suffix = legacy.removeprefix("NQ_") if legacy.startswith("NQ_") else legacy
    return "VIBEARR_" + suffix


def getenv(legacy, default=""):
    canonical = canonical_name(legacy)
    current = os.environ.get(canonical)
    previous = os.environ.get(legacy)
    if current is not None and previous is not None and current != previous:
        message = (
            f"{canonical} overrides conflicting {legacy}; remove the legacy variable."
        )
        warnings.warn(message, RuntimeWarning, stacklevel=2)
    if current is not None:
        return current
    return previous if previous is not None else default
