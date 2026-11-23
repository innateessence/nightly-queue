"""Archive the restored root-owned connection ledger before runner bootstrap."""

import argparse
from contextlib import ExitStack
import ctypes
import fcntl
import json
import os
from pathlib import Path
import stat
from uuid import UUID


DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
RENAME_NOREPLACE = 1


def private_directory(descriptor, uid, gid):
    info = os.fstat(descriptor)
    if (
        not stat.S_ISDIR(info.st_mode)
        or stat.S_IMODE(info.st_mode) != 0o700
        or (info.st_uid, info.st_gid) != (uid, gid)
    ):
        raise ValueError("Runner restore directory ownership or mode is unsafe.")


def root_directory(path):
    if not path.is_absolute() or any(
        parent.is_symlink() for parent in (path, *path.parents)
    ):
        raise ValueError("Runner restore requires direct volume roots without links.")
    descriptor = os.open(path, DIRECTORY)
    try:
        private_directory(descriptor, 10001, 10001)
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def private_file(parent, name):
    descriptor = os.open(
        name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent
    )
    info = os.fstat(descriptor)
    if (
        not stat.S_ISREG(info.st_mode)
        or stat.S_IMODE(info.st_mode) != 0o600
        or (info.st_uid, info.st_gid) != (10001, 10001)
        or info.st_nlink != 1
        or info.st_size > 65536
    ):
        os.close(descriptor)
        raise ValueError("Runner restore marker or lock is not a private bounded file.")
    return descriptor


def history_directory(parent, name):
    created = False
    try:
        os.mkdir(name, 0o700, dir_fd=parent)
        created = True
    except FileExistsError:
        pass
    descriptor = os.open(name, DIRECTORY, dir_fd=parent)
    try:
        if created:
            os.fchown(descriptor, 10001, 10001)
        private_directory(descriptor, 10001, 10001)
        os.fsync(descriptor)
        os.fsync(parent)
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def connection_directory(parent):
    try:
        descriptor = os.open("connections", DIRECTORY, dir_fd=parent)
    except FileNotFoundError:
        return None
    try:
        private_directory(descriptor, 0, 0)
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def archive_connections(source, target):
    rename = ctypes.CDLL(None, use_errno=True).renameat2
    rename.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    rename.restype = ctypes.c_int
    if rename(source, b"connections", target, b"connections", RENAME_NOREPLACE) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))


def recover(identity, secret_root, state_root):
    if str(UUID(identity)) != identity or os.geteuid() != 0:
        raise ValueError(
            "Runner connection restore requires an exact UUID and trusted root job."
        )
    with ExitStack() as stack:

        def retain(descriptor):
            stack.callback(os.close, descriptor)
            return descriptor

        secrets = retain(root_directory(secret_root))
        state = retain(root_directory(state_root))
        lock = retain(private_file(secrets, "bootstrap.lock"))
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ValueError(
                "Another runner bootstrap owns these restored volumes."
            ) from error
        marker = retain(private_file(secrets, "restore-required.json"))
        required = json.loads(os.read(marker, 65537))
        if required != {"backup_id": identity, "new_runner_identity_required": True}:
            raise ValueError("Select the exact pending runner restore marker.")
        history = retain(history_directory(state, "restore-history"))
        selected = retain(history_directory(history, identity))
        source = connection_directory(state)
        if source is not None:
            retain(source)
        archived = connection_directory(selected)
        if archived is not None:
            retain(archived)
        if source is not None and archived is not None:
            raise ValueError(
                "Runner connection archive already exists; preserve both states."
            )
        if source is not None:
            before = os.fstat(source)
            current = os.stat("connections", dir_fd=state, follow_symlinks=False)
            if (before.st_dev, before.st_ino) != (current.st_dev, current.st_ino):
                raise ValueError("Restored connection state changed before archival.")
            archive_connections(state, selected)
            os.fsync(selected)
            os.fsync(state)
        return {
            "backup_id": identity,
            "connections_archived": source is not None or archived is not None,
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("backup_id")
    options = parser.parse_args()
    print(
        json.dumps(
            recover(
                options.backup_id,
                Path("/restore/runner-secrets"),
                Path("/restore/runner-data"),
            )
        )
    )


if __name__ == "__main__":
    main()
