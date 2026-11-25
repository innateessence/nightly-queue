"""Initialize named volumes without replacing existing credentials."""

import os
from pathlib import Path
import secrets
import stat
import tempfile


ROOT = Path("/run/vibearr")


def private(directory, uid=10001):
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory.chmod(0o700)
    os.chown(directory, uid, uid)


def write_once(path, value, uid=10001, gid=None, mode=0o600):
    if path.exists():
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o027:
            raise RuntimeError("Existing credential has unsafe permissions or type.")
        current = path.read_text().strip()
        if not current or (value and not secrets.compare_digest(current, value)):
            raise RuntimeError(
                "Credential conflicts with saved volume; rotate explicitly."
            )
        return
    descriptor, temporary = tempfile.mkstemp(prefix=".credential-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as stream:
            stream.write((value or secrets.token_urlsafe(48)) + "\n")
            stream.flush()
            os.fchmod(stream.fileno(), mode)
            os.fchown(stream.fileno(), uid, uid if gid is None else gid)
            os.fsync(stream.fileno())
        os.link(temporary, path, follow_symlinks=False)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(temporary).unlink(missing_ok=True)


def require_existing_credentials(root, database):
    if not (database / "PG_VERSION").exists():
        return
    required = {
        "database/password": os.environ.get("VIBEARR_POSTGRES_PASSWORD", ""),
        "application/api-token": os.environ.get("VIBEARR_API_TOKEN", ""),
        "application/encryption-key": os.environ.get("VIBEARR_CREDENTIAL_KEY", ""),
        "application/django-key": os.environ.get("DJANGO_SECRET_KEY", ""),
    }
    missing = [
        name
        for name, supplied in required.items()
        if not (root / name).is_file() and not supplied
    ]
    if missing:
        raise RuntimeError(
            "Existing PostgreSQL data requires explicit original credentials: "
            + ", ".join(missing)
        )
