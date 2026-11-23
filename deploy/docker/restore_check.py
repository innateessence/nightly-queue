"""Verify replacement runner authority against the disposable restored web API."""

import argparse
import hashlib
import json
import os
from pathlib import Path
from uuid import UUID

from vibearr_runner.client import AuthenticationError, Client


def login_digest():
    path = Path("/logins/disposable-restore-witness")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def seed_state():
    state = Path("/var/lib/vibearr")
    witness = state / "backup-state-witness.json"
    if witness.exists():
        raise RuntimeError("Preserve the existing recovery witness.")
    path = Path("/logins/disposable-restore-witness")
    with path.open("xb") as stream:
        stream.write(b"Synthetic login storage witness; no provider authority.\n")
    path.chmod(0o600)
    os.chown(path, 10001, 10001)
    value = {"login_sha256": login_digest()}
    witness.write_text(json.dumps(value))
    witness.chmod(0o600)
    os.chown(witness, 10001, 10001)
    return {"kind": "actual-recovery-state-seed", "synthetic_login": True}


def verify_worker(identity):
    from vibearr_executor.client import Client as WorkerClient, ExecutorRejected

    root = Path("/run/vibearr/worker")
    history = Path("/control/restore-history") / identity
    old = (root / ("retired-control-token." + identity)).read_text().strip()
    current = (root / "control-token").read_text().strip()
    retired_mediation = (
        (root / ("retired-mediation-token." + identity)).read_text().strip()
    )
    mediation = (root / "mediation-token").read_text().strip()
    if (
        old == current
        or retired_mediation == mediation
        or (root / "restore-required.json").exists()
    ):
        raise RuntimeError("Restored worker channel authority was not replaced.")
    if not (history / "instance").is_file():
        raise RuntimeError("Restored worker authority history is missing.")
    try:
        WorkerClient("http://worker:8791", old).capabilities()
    except ExecutorRejected as error:
        if error.http_status != 401:
            raise
    else:
        raise RuntimeError("Restored worker accepted its retired control token.")
    observed = WorkerClient("http://worker:8791", current).capabilities()
    if observed["instance_id"] == (history / "instance").read_text().strip():
        raise RuntimeError("Restored worker reused its retired instance authority.")
    return {
        "kind": "actual-restored-worker-authority",
        "backup_id": identity,
        "old_token_rejected": True,
        "new_token_authenticated": True,
        "mediation_token_rotated": True,
        "worker_control_archived": True,
        "instance_id": observed["instance_id"],
        "worker_boot": observed["worker_boot"],
        "qualified": observed["qualified"],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("backup_id")
    parser.add_argument("--worker", action="store_true")
    options = parser.parse_args()
    if os.environ.get("VIBEARR_DISPOSABLE_ACCEPTANCE") != "1" or (
        options.backup_id != "seed-state"
        and str(UUID(options.backup_id)) != options.backup_id
    ):
        raise RuntimeError(
            "Select an exact backup UUID in a disposable acceptance project."
        )
    if options.backup_id == "seed-state":
        print(json.dumps(seed_state()))
        return
    if options.worker:
        print(json.dumps(verify_worker(options.backup_id)))
        return
    root = Path("/run/vibearr/runner")
    old = (root / ("retired-api-token." + options.backup_id)).read_text().strip()
    token = (root / "api-token").read_text().strip()
    history = Path("/var/lib/vibearr/restore-history") / options.backup_id
    if (
        old == token
        or (root / "restore-required.json").exists()
        or not history.is_dir()
        or not (history / "journal.json").is_file()
    ):
        raise RuntimeError(
            "Restored authority was not replaced with a fresh active identity."
        )
    try:
        Client("http://app:8000", old).configuration()
    except AuthenticationError:
        pass
    else:
        raise RuntimeError("The restored API accepted the retired runner token.")
    configuration = Client("http://app:8000", token).configuration()
    if configuration["global"]["enabled"]:
        raise RuntimeError(
            "Restored processing was enabled without release acceptance."
        )
    saved = json.loads((history / "backup-state-witness.json").read_bytes())
    if saved["login_sha256"] != login_digest():
        raise RuntimeError(
            "Restored login storage differs from the original private state."
        )
    active = Path("/var/lib/vibearr/journal.json")
    if active.exists():
        current = json.loads(active.read_bytes())
        previous = json.loads((history / "journal.json").read_bytes())
        if current.get("operation") or current.get("runner_id") == previous.get(
            "runner_id"
        ):
            raise RuntimeError("Restored controller reused retired journal authority.")
    print(
        json.dumps(
            {
                "kind": "disposable-restore-authority-check",
                "backup_id": options.backup_id,
                "old_token_rejected": True,
                "new_token_authenticated": True,
                "processing_enabled": False,
                "controller_journal_archived": (history / "journal.json").is_file(),
                "login_state_preserved": True,
                "synthetic_login": True,
            }
        )
    )


if __name__ == "__main__":
    main()
