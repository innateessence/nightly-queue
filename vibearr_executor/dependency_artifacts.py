"""Bounded dependency layers never write outside their dedicated image prefix."""

import io
import os
from pathlib import Path, PurePosixPath
import stat
import tarfile

from .contracts import Rejected, digest

MAX_DEPENDENCIES = 256 * 1024 * 1024
MAX_DEPENDENCY_FILES = 50000


def read_layer(content: bytes) -> dict[str, tuple[int, bytes]]:
    if len(content) > MAX_DEPENDENCIES:
        raise Rejected("Dependency layer exceeds its byte limit.")
    files: dict[str, tuple[int, bytes]] = {}
    size = 0
    try:
        with tarfile.open(fileobj=io.BytesIO(content), mode="r:") as archive:
            for member in archive:
                path = PurePosixPath(member.name)
                safe = (
                    str(path) == member.name
                    and not path.is_absolute()
                    and all(part not in {"..", ".", ".git"} for part in path.parts)
                )
                permitted = member.name in {
                    "requirements.txt",
                    "resolved.txt",
                } or member.name.startswith("venv/")
                if (
                    not safe
                    or not permitted
                    or "\\" in member.name
                    or "\x00" in member.name
                    or len(member.name.encode()) > 512
                    or member.name in files
                ):
                    raise Rejected("Invalid dependency layer path.")
                if not member.isfile() or member.mode & ~0o777 or member.size < 0:
                    raise Rejected(
                        "Dependency layers allow bounded regular files only."
                    )
                size += member.size
                if size > MAX_DEPENDENCIES or len(files) >= MAX_DEPENDENCY_FILES:
                    raise Rejected("Dependency layer exceeds capacity.")
                stream = archive.extractfile(member)
                if stream is None:
                    raise Rejected("Dependency layer content is missing.")
                value = stream.read(member.size + 1)
                if len(value) != member.size:
                    raise Rejected("Dependency layer is truncated.")
                files[member.name] = (0o755 if member.mode & 0o111 else 0o644, value)
    except (tarfile.TarError, EOFError, OSError):
        raise Rejected(
            "Dependency layer is not a valid uncompressed archive."
        ) from None
    if not {
        "requirements.txt",
        "resolved.txt",
        "venv/pyvenv.cfg",
        "venv/bin/python",
    } <= set(files):
        raise Rejected(
            "Dependency layer lacks its fixed environment and provenance files."
        )
    for name in files:
        if any(str(parent) in files for parent in PurePosixPath(name).parents):
            raise Rejected("Dependency layer file is also a directory.")
    configuration = files["venv/pyvenv.cfg"][1].decode().splitlines()
    settings = [
        line.split("=", 1)[1].strip()
        for line in configuration
        if line.split("=", 1)[0].strip() == "include-system-site-packages"
    ]
    if settings != ["false"]:
        raise Rejected(
            "Candidate dependencies must remain separate from protected tools."
        )
    return files


def capture(directory: Path, requirements: bytes) -> bytes:
    output = io.BytesIO()
    total = 0
    count = 0
    with tarfile.open(fileobj=output, mode="w", format=tarfile.PAX_FORMAT) as archive:
        entries = {"requirements.txt": (0o644, requirements)}
        for parent, directories, names in os.walk(directory, followlinks=False):
            for name in directories:
                if not stat.S_ISDIR((Path(parent) / name).lstat().st_mode):
                    raise Rejected(
                        "Dependency environment contains a linked directory."
                    )
            for name in names:
                path = Path(parent) / name
                descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                with os.fdopen(descriptor, "rb") as stream:
                    info = os.fstat(stream.fileno())
                    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                        raise Rejected(
                            "Dependency environment contains a link or special file."
                        )
                    total += info.st_size
                    count += 1
                    if total > MAX_DEPENDENCIES or count > MAX_DEPENDENCY_FILES:
                        raise Rejected("Dependency environment exceeds capacity.")
                    content = stream.read(info.st_size + 1)
                if len(content) != info.st_size:
                    raise Rejected("Dependency environment changed during capture.")
                relative = path.relative_to(directory).as_posix()
                target = (
                    "resolved.txt" if relative == "resolved.txt" else "venv/" + relative
                )
                entries[target] = (0o755 if info.st_mode & 0o111 else 0o644, content)
        for name, (mode, content) in sorted(entries.items()):
            entry = tarfile.TarInfo(name)
            entry.size, entry.mode = len(content), mode
            archive.addfile(entry, io.BytesIO(content))
    value = output.getvalue()
    read_layer(value)
    return value


def manifest(content: bytes) -> dict:
    files = read_layer(content)
    return {
        "schema_version": 1,
        "sha256": digest(content),
        "bytes": len(content),
        "files": len(files),
        "requirements_digest": digest(files["requirements.txt"][1]),
    }
