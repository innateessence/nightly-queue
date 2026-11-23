"""Explicitly retire a restored worker's authority before its first new boot."""

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import secrets
import stat
from uuid import UUID

from .backup_files import read_json


TOKENS = ("control-token", "mediation-token")


def registered_channel(path, info):
    return (
        path.parent == Path("/restore/worker-secrets")
        and path.name in TOKENS
        and stat.S_ISREG(info.st_mode)
        and stat.S_IMODE(info.st_mode) == 0o440
        and info.st_uid == 0
        and info.st_gid == 10001
    )


def sync(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def write_private(path, value):
    temporary = path.with_name("." + path.name + "." + secrets.token_hex(8) + ".new")
    descriptor = os.open(
        temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
    )
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(value)
        stream.flush()
        os.fsync(stream.fileno())
    os.chown(temporary, 10001, 10001)
    temporary.replace(path)
    sync(path.parent)


def token(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        private = not info.st_mode & 0o077
        if (
            not stat.S_ISREG(info.st_mode)
            or not (private or registered_channel(path, info))
            or info.st_size > 4096
        ):
            raise ValueError("Worker token must be a private bounded regular file.")
        value = stream.read().decode().strip()
    if len(value) < 32 or any(character.isspace() for character in value):
        raise ValueError("Worker token is invalid.")
    return value


def sha(value):
    return hashlib.sha256(value.encode()).hexdigest()


def recover(identity, secret_root, control_root):
    if str(UUID(identity)) != identity or os.geteuid() != 0:
        raise ValueError(
            "Worker restore requires an exact backup UUID and trusted root job."
        )
    for root in (secret_root, control_root):
        if root.is_symlink() or not root.is_dir():
            raise ValueError("Worker restore requires explicit private volume roots.")
    descriptor = os.open(
        secret_root / ".restore-lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600
    )
    with os.fdopen(descriptor, "r+") as lock:
        info = os.fstat(lock.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
            raise ValueError("Worker restore lock is not private.")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ValueError("Another worker restore owns these volumes.") from error
        return recover_locked(identity, secret_root, control_root)


def recover_locked(identity, secret_root, control_root):
    marker = secret_root / "restore-required.json"
    intent_path = secret_root / ("restore-intent." + identity + ".json")
    complete_path = secret_root / ("restore-completed." + identity + ".json")
    if not marker.exists():
        if not complete_path.exists():
            raise ValueError("Worker restore marker is missing.")
        completed = read_json(complete_path)
        if (
            completed.get("backup_id") != identity
            or completed.get("retired") is not True
        ):
            raise ValueError("Worker restore completion does not bind this backup.")
        for name in TOKENS:
            if sha(token(secret_root / name)) != completed["token_digests"][name]:
                raise ValueError(
                    "Worker authority changed after the completed restore."
                )
        return completed
    required = read_json(marker)
    if required != {"backup_id": identity, "new_worker_identity_required": True}:
        raise ValueError("Worker restore marker does not bind this backup.")
    if intent_path.exists():
        intent = read_json(intent_path)
    else:
        children = sorted(
            path.name
            for path in control_root.iterdir()
            if path.name != "restore-history"
        )
        intent = {
            "backup_id": identity,
            "control_entries": children,
            "old_token_digests": {
                name: sha(token(secret_root / name)) for name in TOKENS
            },
            "new_tokens": {name: secrets.token_hex(32) for name in TOKENS},
        }
        write_private(intent_path, (json.dumps(intent, sort_keys=True) + "\n").encode())
    if intent.get("backup_id") != identity or set(intent.get("new_tokens", {})) != set(
        TOKENS
    ):
        raise ValueError("Worker restore intent is invalid.")
    history = control_root / "restore-history"
    if history.is_symlink():
        raise ValueError("Worker restore history cannot be a link.")
    history.mkdir(mode=0o700, exist_ok=True)
    archive = history / identity
    if archive.is_symlink():
        raise ValueError("Worker restore archive cannot be a link.")
    archive.mkdir(mode=0o700, exist_ok=True)
    for name in intent["control_entries"]:
        if (
            not isinstance(name, str)
            or name in {"", ".", "..", "restore-history"}
            or Path(name).name != name
        ):
            raise ValueError("Worker restore intent has an unsafe control entry.")
        source, target = control_root / name, archive / name
        if source.is_symlink() or target.is_symlink():
            raise ValueError("Worker control authority cannot contain links.")
        if source.exists():
            if target.exists():
                raise ValueError("Worker restore would overwrite archived authority.")
            source.rename(target)
        elif not target.exists():
            raise ValueError("Worker control authority disappeared during restore.")
    sync(archive)
    sync(control_root)
    if {path.name for path in control_root.iterdir()} != {"restore-history"}:
        raise ValueError("Worker control state changed while restoring authority.")
    token_digests = {}
    for name in TOKENS:
        old = token(secret_root / name)
        new = intent["new_tokens"][name]
        if (
            not isinstance(new, str)
            or len(new) != 64
            or any(character not in "0123456789abcdef" for character in new)
        ):
            raise ValueError("Worker restore intent has an invalid replacement token.")
        if sha(old) not in {intent["old_token_digests"][name], sha(new)}:
            raise ValueError("Worker token changed outside the restore operation.")
        retired = secret_root / ("retired-" + name + "." + identity)
        if retired.exists():
            if sha(token(retired)) != intent["old_token_digests"][name]:
                raise ValueError(
                    "Retired worker token differs from the restore intent."
                )
        elif sha(old) == intent["old_token_digests"][name]:
            write_private(retired, (old + "\n").encode())
        else:
            raise ValueError("Retired worker token was lost during restore.")
        if old != new:
            write_private(secret_root / name, (new + "\n").encode())
        token_digests[name] = sha(new)
    if len(set(token_digests.values())) != len(TOKENS):
        raise ValueError("Worker control and mediation tokens must be distinct.")
    completed = {"backup_id": identity, "retired": True, "token_digests": token_digests}
    write_private(
        complete_path, (json.dumps(completed, sort_keys=True) + "\n").encode()
    )
    marker.unlink()
    sync(secret_root)
    return completed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("backup_id")
    options = parser.parse_args()
    result = recover(
        options.backup_id,
        Path("/restore/worker-secrets"),
        Path("/restore/worker-control"),
    )
    print(
        json.dumps(
            {
                "backup_id": result["backup_id"],
                "worker_authority_retired": True,
                "worker_qualification_required": True,
            }
        )
    )


if __name__ == "__main__":
    main()
