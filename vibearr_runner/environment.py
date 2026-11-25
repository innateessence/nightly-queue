"""Environment names shared by the app and standalone clients."""

import os
import stat
from pathlib import Path


def getenv(name, default=""):
    current = os.environ.get(name)
    filename = os.environ.get(name + "_FILE")
    if not filename:
        return current if current is not None else default
    if (
        name == "VIBEARR_RUNNER_API_TOKEN"
        and (Path(filename).parent / "restore-required.json").exists()
    ):
        raise ValueError(
            "Restored state requires a fresh Docker runner identity before startup."
        )
    if current is not None:
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
