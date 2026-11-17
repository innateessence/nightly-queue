"""Verify replacement runner authority against the disposable restored web API."""

import argparse
import json
import os
from pathlib import Path
from uuid import UUID

from vibearr_runner.client import AuthenticationError, Client


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("backup_id")
    options = parser.parse_args()
    if (
        os.environ.get("VIBEARR_DISPOSABLE_ACCEPTANCE") != "1"
        or str(UUID(options.backup_id)) != options.backup_id
    ):
        raise RuntimeError(
            "Select an exact backup UUID in a disposable acceptance project."
        )
    root = Path("/run/vibearr/runner")
    old = (root / ("retired-api-token." + options.backup_id)).read_text().strip()
    token = (root / "api-token").read_text().strip()
    history = Path("/var/lib/vibearr/restore-history") / options.backup_id
    if (
        old == token
        or (root / "restore-required.json").exists()
        or not history.is_dir()
    ):
        raise RuntimeError(
            "Restored authority was not replaced with a fresh active identity."
        )
    try:
        Client("http://vibearr:8000", old).configuration()
    except AuthenticationError:
        pass
    else:
        raise RuntimeError("The restored API accepted the retired runner token.")
    configuration = Client("http://vibearr:8000", token).configuration()
    if configuration["global"]["enabled"]:
        raise RuntimeError(
            "Restored processing was enabled without release acceptance."
        )
    print(
        json.dumps(
            {
                "kind": "disposable-restore-authority-check",
                "backup_id": options.backup_id,
                "old_token_rejected": True,
                "new_token_authenticated": True,
                "processing_enabled": False,
            }
        )
    )


if __name__ == "__main__":
    main()
