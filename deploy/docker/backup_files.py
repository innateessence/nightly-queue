"""Bounded volume snapshots never restore links or write outside empty targets."""

import hashlib
import json
import os
from pathlib import PurePosixPath
import shutil
import stat
import tarfile


VOLUMES = (
    "application-secrets",
    "database-secrets",
    "runner-secrets",
    "runner-data",
    "runner-control",
    "runner-logins",
)
MAX_FILES = 250000
MAX_BYTES = 64 * 1024**3


def checksum(path):
    value = hashlib.sha256()
    with path.open("rb") as stream:
        while content := stream.read(1024**2):
            value.update(content)
    return value.hexdigest()


def safe_path(name):
    value = PurePosixPath(name)
    if (
        not name
        or str(value) != name
        or value.is_absolute()
        or any(part in {".", ".."} for part in value.parts)
        or value.parts[0] not in VOLUMES
        or "\\" in name
        or "\x00" in name
        or len(name.encode()) > 4096
    ):
        raise ValueError("Backup contains an unsafe volume path.")
    return value


def inventory(root):
    records, total = [], 0
    for volume in VOLUMES:
        directory = root / volume
        paths = [directory, *sorted(directory.rglob("*"))]
        for path in paths:
            info = path.lstat()
            regular, is_directory = stat.S_ISREG(info.st_mode), stat.S_ISDIR(
                info.st_mode
            )
            allowed_mode = 0o1777 if is_directory else 0o777
            if (
                not (regular or is_directory)
                or stat.S_IMODE(info.st_mode) & ~allowed_mode
            ):
                raise ValueError(
                    "Backups require regular files and directories without links."
                )
            size = info.st_size if regular else 0
            total += size
            if total > MAX_BYTES or len(records) >= MAX_FILES:
                raise ValueError("Backup volume snapshot exceeds its fixed bounds.")
            name = path.relative_to(root).as_posix()
            safe_path(name)
            records.append(
                {
                    "path": name,
                    "kind": "file" if regular else "directory",
                    "mode": stat.S_IMODE(info.st_mode),
                    "uid": info.st_uid,
                    "gid": info.st_gid,
                    "bytes": size,
                    "sha256": checksum(path) if regular else None,
                }
            )
    return records


def capture(root, destination):
    before = inventory(root)
    with tarfile.open(destination, "w", format=tarfile.PAX_FORMAT) as archive:
        for entry in before:
            path = root / entry["path"]
            info = tarfile.TarInfo(entry["path"])
            info.mode, info.uid, info.gid = entry["mode"], entry["uid"], entry["gid"]
            if entry["kind"] == "directory":
                info.type = tarfile.DIRTYPE
                archive.addfile(info)
            else:
                info.size = entry["bytes"]
                with path.open("rb") as stream:
                    archive.addfile(info, stream)
    if inventory(root) != before:
        raise ValueError(
            "State changed while backing up; stop all state writers first."
        )
    return before


def verify_archive(archive, records):
    if not isinstance(records, list) or not len(VOLUMES) <= len(records) <= MAX_FILES:
        raise ValueError("Backup inventory is missing or exceeds its bounds.")
    entries = {value["path"]: value for value in records}
    if len(entries) != len(records) or not set(VOLUMES) <= set(entries):
        raise ValueError("Backup inventory omits a volume or repeats a path.")
    for name, entry in entries.items():
        if type(entry["bytes"]) is not int or not 0 <= entry["bytes"] <= MAX_BYTES:
            raise ValueError("Backup file size is invalid.")
        value = safe_path(name)
        parents = [str(parent) for parent in value.parents if str(parent) != "."]
        if any(
            entries.get(parent, {}).get("kind") != "directory" for parent in parents
        ):
            raise ValueError("Backup path has no declared directory parent.")
        if name in VOLUMES and entry["kind"] != "directory":
            raise ValueError("Every backup volume root must be a directory.")
    seen, total = set(), 0
    for member in archive:
        name = str(safe_path(member.name))
        if name in seen or name not in entries:
            raise ValueError("Backup archive differs from its inventory.")
        expected = entries[name]
        regular = expected["kind"] == "file"
        if (
            (not member.isfile() if regular else not member.isdir())
            or expected["kind"] not in {"file", "directory"}
            or (member.mode, member.uid, member.gid, member.size)
            != (expected["mode"], expected["uid"], expected["gid"], expected["bytes"])
            or member.mode & ~(0o777 if regular else 0o1777)
        ):
            raise ValueError("Backup entry type, ownership or mode differs.")
        total += member.size
        if total > MAX_BYTES:
            raise ValueError("Expanded backup exceeds its byte limit.")
        seen.add(name)
        if regular:
            stream = archive.extractfile(member)
            if stream is None:
                raise ValueError("Backup file content is missing.")
            value, size = hashlib.sha256(), 0
            while content := stream.read(1024**2):
                value.update(content)
                size += len(content)
            if size != expected["bytes"] or value.hexdigest() != expected["sha256"]:
                raise ValueError("Backup file digest differs from its inventory.")
    if seen != set(entries):
        raise ValueError("Backup archive is incomplete.")


def require_capacity(root, records, database_bytes):
    if type(database_bytes) is not int or database_bytes < 0:
        raise ValueError(
            "This backup lacks its database size estimate; create an updated snapshot."
        )
    if any(type(entry["bytes"]) is not int or entry["bytes"] < 0 for entry in records):
        raise ValueError("Backup file size is invalid.")
    required = sum(entry["bytes"] for entry in records) + len(records) * 8192
    required += database_bytes * 2 + 256 * 1024**2 + 1024**3
    for name in VOLUMES:
        if shutil.disk_usage(root / name).free < required:
            raise ValueError(
                "Restore storage cannot fit this snapshot and a 1 GiB reserve."
            )
    return required


def restore(source, records, root):
    for name in VOLUMES:
        directory = root / name
        if directory.is_symlink() or not directory.is_dir() or any(directory.iterdir()):
            raise ValueError(
                "Restore requires an empty, dedicated target for every volume."
            )
    with tarfile.open(source, "r:") as archive:
        verify_archive(archive, records)
    with tarfile.open(source, "r:") as archive:
        for member in archive:
            target = root / str(safe_path(member.name))
            if member.isdir():
                target.mkdir(mode=0o700, exist_ok=True)
                continue
            stream = archive.extractfile(member)
            if stream is None:
                raise ValueError("Backup file content is missing.")
            descriptor = os.open(
                target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
            )
            with os.fdopen(descriptor, "wb") as output:
                while content := stream.read(1024**2):
                    output.write(content)
                output.flush()
                os.fsync(output.fileno())
    for entry in sorted(
        records, key=lambda value: len(PurePosixPath(value["path"]).parts), reverse=True
    ):
        target = root / entry["path"]
        os.chown(target, entry["uid"], entry["gid"])
        target.chmod(entry["mode"])
        descriptor = os.open(target, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    if inventory(root) != records:
        raise ValueError("Restored volume inventory differs from the backup.")


def read_json(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > 64 * 1024**2:
            raise ValueError("Backup metadata must be a bounded regular file.")
        return json.load(stream)
