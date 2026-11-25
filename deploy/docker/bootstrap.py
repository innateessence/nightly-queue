"""Initialize declared app volumes, then drop setup privileges."""

import base64
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import secrets
import stat
import sys
import time

from .environment import load
from .initialize import (
    ROOT,
    private,
    require_existing_credentials,
    write_once,
)


@contextmanager
def initialization_lock(directory, timeout=120):
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not stat.S_ISDIR(directory.lstat().st_mode):
        raise RuntimeError("Initialization lock requires a private directory.")
    flags = os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK
    descriptor = os.open(directory / ".initialize.lock", flags, 0o600)
    with os.fdopen(descriptor, "w") as lock:
        info = os.fstat(lock.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
            raise RuntimeError("Initialization lock has unsafe type or owner.")
        if info.st_mode & 0o077 or info.st_nlink != 1:
            raise RuntimeError("Initialization lock has unsafe permissions or links.")
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise RuntimeError(
                        "Credential initialization lock timed out."
                    ) from None
                time.sleep(0.1)
        yield


def initialize(root=ROOT, existing_database=Path("/var/lib/existing-postgres")):
    os.umask(0o077)
    with initialization_lock(root / "application"):
        require_existing_credentials(root, existing_database)
        for name in ("application", "database", "runner"):
            private(root / name)
        database = root / "database"
        database.chmod(0o750)
        os.chown(database, 999, 10001)
        write_once(
            database / "password",
            os.environ.get("VIBEARR_POSTGRES_PASSWORD", ""),
            uid=999,
            gid=10001,
            mode=0o440,
        )
        write_once(
            root / "application/api-token",
            os.environ.get("VIBEARR_API_TOKEN", ""),
        )
        encryption_key = os.environ.get("VIBEARR_CREDENTIAL_KEY", "")
        if not encryption_key and not (root / "application/encryption-key").exists():
            encryption_key = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()
        write_once(root / "application/encryption-key", encryption_key)
        write_once(
            root / "application/django-key", os.environ.get("DJANGO_SECRET_KEY", "")
        )
        worker = root / "worker"
        if worker.exists():
            private(worker, uid=0)
            os.chown(worker, 0, 10001)
            worker.chmod(0o750)
            for name in ("control-token", "mediation-token"):
                write_once(worker / name, "", uid=0, gid=10001, mode=0o440)


def main():
    if os.getuid() == 0:
        initialize()
        os.setgroups([10001])
        os.setgid(10001)
        os.setuid(10001)
    load()
    if len(sys.argv) > 1:
        os.execvp(sys.argv[1], sys.argv[1:])
    from .application import main as application

    raise SystemExit(application())


if __name__ == "__main__":
    main()
