"""Bounded reads of explicit private operator files."""

import json
import os
from pathlib import Path
import stat


def private_json(path: Path, maximum=8388608):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
            or info.st_size > maximum
        ):
            raise ValueError("Select a bounded, private, owned regular JSON file.")
        content = stream.read(maximum + 1)
        if len(content) > maximum:
            raise ValueError("Private operator JSON exceeds its bounded size.")
        return json.loads(content)
