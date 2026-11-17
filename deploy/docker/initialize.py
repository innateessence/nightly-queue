"""Initialize named volumes without replacing existing credentials."""

import base64
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


def selected(canonical, legacy):
    return os.environ.get(canonical) or os.environ.get(legacy, "")


def main():
    os.umask(0o077)
    require_existing_credentials(ROOT, Path("/var/lib/existing-postgres"))
    for name in (
        "application",
        "database",
        "runner",
        "agent",
        "setup",
        "validation",
        "agent-database",
        "validation-database",
    ):
        private(ROOT / name)
    database = ROOT / "database"
    database.chmod(0o750)
    os.chown(database, 999, 10001)
    write_once(
        database / "password",
        selected("VIBEARR_POSTGRES_PASSWORD", "POSTGRES_PASSWORD"),
        uid=999,
        gid=10001,
        mode=0o440,
    )
    for name in ("agent-database", "validation-database"):
        directory = ROOT / name
        directory.chmod(0o750)
        os.chown(directory, 999, 10001)
        write_once(directory / "password", "", uid=999, gid=10001, mode=0o440)
    write_once(
        ROOT / "application/api-token", selected("VIBEARR_API_TOKEN", "NQ_API_TOKEN")
    )
    key = selected("VIBEARR_CREDENTIAL_KEY", "NQ_CREDENTIAL_KEY")
    path = ROOT / "application/encryption-key"
    if not key and not path.exists():
        key = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()
    write_once(path, key)
    write_once(ROOT / "application/django-key", os.environ.get("DJANGO_SECRET_KEY", ""))
    for name in ("agent", "setup", "validation"):
        write_once(ROOT / name / "control-token", "", uid=0, gid=10001, mode=0o440)
        private(ROOT / name, uid=0)
        os.chown(ROOT / name, 0, 10001)
        (ROOT / name).chmod(0o750)
    write_once(ROOT / "runner/control-token", "")
    for directory in (
        Path("/var/lib/vibearr"),
        Path("/var/lib/vibearr-backups"),
        Path("/var/lib/gateway"),
        Path("/var/lib/vibearr-browser"),
        Path("/var/lib/vibearr-checks"),
    ):
        private(directory)
    print("Docker volumes initialized; saved credentials preserved.")


def require_existing_credentials(root, database):
    if not (database / "PG_VERSION").exists():
        return
    required = {
        "database/password": selected("VIBEARR_POSTGRES_PASSWORD", "POSTGRES_PASSWORD"),
        "application/api-token": selected("VIBEARR_API_TOKEN", "NQ_API_TOKEN"),
        "application/encryption-key": selected(
            "VIBEARR_CREDENTIAL_KEY", "NQ_CREDENTIAL_KEY"
        ),
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


if __name__ == "__main__":
    main()
