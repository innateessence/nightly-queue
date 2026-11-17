"""Environment names shared by the app and standalone clients."""

import os
import stat
import warnings
from pathlib import Path


def canonical_name(legacy):
    suffix = legacy.removeprefix("NQ_") if legacy.startswith("NQ_") else legacy
    return "VIBEARR_" + suffix


def getenv(legacy, default=""):
    canonical = canonical_name(legacy)
    current = os.environ.get(canonical)
    previous = os.environ.get(legacy)
    filename = os.environ.get(canonical + "_FILE")
    if filename:
        if (
            canonical == "VIBEARR_RUNNER_API_TOKEN"
            and (Path(filename).parent / "restore-required.json").exists()
        ):
            raise ValueError(
                "Restored state requires a fresh Docker runner identity before startup."
            )
        if current is not None or previous is not None:
            raise ValueError(
                "Select an environment credential or a credential file, not both."
            )
        descriptor = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor) as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
                raise ValueError("Runner credential file must be private.")
            value = stream.read(16385).strip()
        if not value or len(value) > 16384:
            raise ValueError("Runner credential file is empty or oversized.")
        return value
    if current is not None and previous is not None and current != previous:
        message = (
            f"{canonical} overrides conflicting {legacy}; remove the legacy variable."
        )
        warnings.warn(message, RuntimeWarning, stacklevel=2)
    if current is not None:
        return current
    return previous if previous is not None else default
