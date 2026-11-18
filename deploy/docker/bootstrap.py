"""Initialize declared app volumes, then drop setup privileges."""

import base64
import os
from pathlib import Path
import secrets
import sys

from .environment import load
from .initialize import (
    ROOT,
    private,
    require_existing_credentials,
    selected,
    write_once,
)


def main():
    if os.getuid() == 0:
        require_existing_credentials(ROOT, Path("/var/lib/existing-postgres"))
        for name in ("application", "database", "runner"):
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
        write_once(
            ROOT / "application/api-token",
            selected("VIBEARR_API_TOKEN", "NQ_API_TOKEN"),
        )
        encryption_key = selected("VIBEARR_CREDENTIAL_KEY", "NQ_CREDENTIAL_KEY")
        if not encryption_key and not (ROOT / "application/encryption-key").exists():
            encryption_key = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()
        write_once(ROOT / "application/encryption-key", encryption_key)
        write_once(
            ROOT / "application/django-key", os.environ.get("DJANGO_SECRET_KEY", "")
        )
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
