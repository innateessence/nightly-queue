"""Load only declared credentials from private Docker volumes."""

import os
import stat
from pathlib import Path


FILES = {
    "database/password": "VIBEARR_POSTGRES_PASSWORD",
    "application/api-token": "VIBEARR_API_TOKEN",
    "application/encryption-key": "VIBEARR_CREDENTIAL_KEY",
    "application/django-key": "DJANGO_SECRET_KEY",
}


def load(root=Path("/run/vibearr")):
    for filename, name in FILES.items():
        path = root / filename
        if not path.exists():
            continue
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor) as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o027:
                raise RuntimeError(
                    "Container credential must be a private regular file."
                )
            value = stream.read(16385).strip()
        if not value or len(value) > 16384:
            raise RuntimeError("Container credential is empty or oversized.")
        os.environ[name] = value
