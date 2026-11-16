import io
import os
import stat
import tarfile
from pathlib import Path, PurePosixPath

from .contracts import MAX_ARCHIVE, MAX_FILES, Rejected, canonical, digest


def safe_path(name: str) -> str:
    path = PurePosixPath(name)
    if not name or len(name.encode()) > 512 or path.is_absolute() or str(path) != name:
        raise Rejected("Noncanonical artifact path.")
    if (
        any(part in {".", "..", ".git"} for part in path.parts)
        or "\\" in name
        or "\x00" in name
    ):
        raise Rejected("Unsafe artifact path or Git metadata.")
    return name


def unpack(data: bytes) -> dict[str, tuple[int, bytes]]:
    if len(data) > MAX_ARCHIVE:
        raise Rejected("Source archive exceeds its byte limit.")
    files: dict[str, tuple[int, bytes]] = {}
    seen: set[str] = set()
    total = 0
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as archive:
            for member in archive:
                name = safe_path(member.name)
                if name in seen or len(seen) >= MAX_FILES:
                    raise Rejected("Duplicate path or too many archive entries.")
                seen.add(name)
                if member.isdir():
                    continue
                if not member.isfile() or member.mode & ~0o777 or member.size < 0:
                    raise Rejected(
                        "Artifacts permit regular files only, without special modes or links."
                    )
                total += member.size
                if total > MAX_ARCHIVE:
                    raise Rejected("Expanded artifact is too large.")
                stream = archive.extractfile(member)
                if stream is None:
                    raise Rejected("Missing archive content.")
                content = stream.read(member.size + 1)
                if len(content) != member.size:
                    raise Rejected("Truncated artifact.")
                files[name] = (0o755 if member.mode & 0o111 else 0o644, content)
    except (tarfile.TarError, EOFError, OSError) as exc:
        raise Rejected("Invalid uncompressed source archive.") from exc
    for name in seen:
        if any(str(parent) in files for parent in PurePosixPath(name).parents):
            raise Rejected("A file is also an archive directory.")
    return files


def pack(files: dict[str, tuple[int, bytes]]) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w", format=tarfile.PAX_FORMAT) as archive:
        for name, (mode, content) in sorted(files.items()):
            safe_path(name)
            entry = tarfile.TarInfo(name)
            entry.size, entry.mode = len(content), mode
            archive.addfile(entry, io.BytesIO(content))
    value = output.getvalue()
    unpack(value)
    return value


def extract(data: bytes, directory: Path) -> None:
    files = unpack(data)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    for name, (mode, content) in files.items():
        target = directory / name
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(
            target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode
        )
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
        target.chmod(mode)


def snapshot(directory: Path) -> bytes:
    files: dict[str, tuple[int, bytes]] = {}
    total = 0
    for parent, directories, names in os.walk(directory, followlinks=False):
        if Path(parent) == directory and ".git" in directories:
            directories.remove(".git")
        for name in directories:
            if not stat.S_ISDIR((Path(parent) / name).lstat().st_mode):
                raise Rejected("Workspace contains a linked directory.")
        for name in names:
            target = Path(parent) / name
            relative = target.relative_to(directory).as_posix()
            if relative == ".git":
                continue
            safe_path(relative)
            descriptor = os.open(target, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(descriptor, "rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise Rejected("Workspace contains a link or special file.")
                total += info.st_size
                if total > MAX_ARCHIVE or len(files) >= MAX_FILES:
                    raise Rejected("Workspace export exceeds artifact bounds.")
                content = stream.read(MAX_ARCHIVE + 1)
            if len(content) != info.st_size:
                raise Rejected("Workspace changed during export.")
            files[relative] = (0o755 if info.st_mode & 0o111 else 0o644, content)
    return pack(files)


def inventory(data: bytes) -> list[dict]:
    return [
        {"path": name, "mode": mode, "size": len(content), "sha256": digest(content)}
        for name, (mode, content) in sorted(unpack(data).items())
    ]


def result_manifest(source: bytes, result: bytes) -> dict:
    before, after = unpack(source), unpack(result)
    return {
        "schema_version": 1,
        "source_digest": digest(source),
        "result_digest": digest(result),
        "files": inventory(result),
        "deleted": sorted(set(before) - set(after)),
    }


def import_result(
    directory: Path, source: bytes, result: bytes, manifest: dict
) -> None:
    if canonical(result_manifest(source, result)) != canonical(manifest):
        raise Rejected("Result digest or file inventory does not match.")
    before, after = unpack(source), unpack(result)
    # Import only after the caller has fenced this trusted checkout from other writers.
    for name in set(before) | set(after):
        target = directory / name
        for parent in (target, *target.parents):
            if parent == directory.parent:
                break
            if parent.is_symlink():
                raise Rejected("Trusted checkout contains a linked import path.")
        if target.is_dir():
            contents = [path for path in target.rglob("*") if not path.is_dir()]
            deleted = set(before) - set(after)
            if any(
                path.is_symlink()
                or path.relative_to(directory).as_posix() not in deleted
                for path in contents
            ):
                raise Rejected("Trusted checkout has a conflicting directory.")
        if name in before:
            mode, content = before[name]
            if not target.exists() or target.read_bytes() != content:
                raise Rejected(
                    "Trusted checkout no longer matches the source artifact."
                )
            if (0o755 if target.stat().st_mode & 0o111 else 0o644) != mode:
                raise Rejected("Trusted checkout file mode changed.")
        elif target.exists() and not target.is_dir():
            raise Rejected("Result would overwrite an unrelated file.")
    for name in sorted(set(before) - set(after), reverse=True):
        target = directory / name
        target.unlink()
        parent = target.parent
        while parent != directory:
            try:
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent
    for name, (mode, content) in after.items():
        target = directory / name
        if target.is_dir():
            for parent in sorted(target.rglob("*"), reverse=True):
                parent.rmdir()
            target.rmdir()
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(
            target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, mode
        )
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
        target.chmod(mode)
