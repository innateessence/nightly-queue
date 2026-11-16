import fcntl
import json
import os
import tempfile
from pathlib import Path
from typing import Any


class JournalError(Exception):
    pass


class ControllerLocked(JournalError):
    pass


class Journal:
    def __init__(self, directory: Path):
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        details = directory.lstat()
        if (
            directory.is_symlink()
            or details.st_uid != os.getuid()
            or details.st_mode & 0o077
        ):
            raise JournalError(
                "Journal directory must be private and owned by the controller."
            )
        self.directory = directory
        self.path = directory / "journal.json"
        self.lock_fd: int | None = None

    def __enter__(self):
        flags = os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC
        descriptor = os.open(self.directory / "controller.lock", flags, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(descriptor)
            raise ControllerLocked("Another controller owns this journal.") from None
        self.lock_fd = descriptor
        return self

    def __exit__(self, *args):
        if self.lock_fd is not None:
            os.close(self.lock_fd)
            self.lock_fd = None

    def load(self) -> dict[str, Any]:
        self._require_lock()
        try:
            descriptor = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return {"version": 1, "runner_id": None, "operation": None}
        with os.fdopen(descriptor) as stream:
            details = os.fstat(stream.fileno())
            if details.st_uid != os.getuid() or details.st_mode & 0o077:
                raise JournalError("Journal file is not private.")
            try:
                data = json.loads(stream.read(8388609))
            except (ValueError, UnicodeError):
                raise JournalError(
                    "Journal is corrupt; operator recovery is required."
                ) from None
        if (
            not isinstance(data, dict)
            or data.get("version") != 1
            or "operation" not in data
        ):
            raise JournalError(
                "Journal format is unsupported; operator recovery is required."
            )
        return data

    def save(self, state: dict[str, Any]) -> None:
        self._require_lock()
        encoded = json.dumps(state, allow_nan=False).encode()
        if len(encoded) > 8388608:
            raise JournalError("Journal exceeds size limit.")
        descriptor, temporary = tempfile.mkstemp(prefix=".journal-", dir=self.directory)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            directory_fd = os.open(self.directory, os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def prepare(
        self, state: dict[str, Any], run_id: str, kind: str, payload: dict[str, Any]
    ) -> None:
        if state.get("operation"):
            raise JournalError(
                "Resolve the existing operation before starting another."
            )
        operation = {
            "run_id": run_id,
            "kind": kind,
            "payload": payload,
            "status": "pending",
        }
        candidate = {**state, "operation": operation}
        self.save(candidate)
        state.update(candidate)

    def _require_lock(self) -> None:
        if self.lock_fd is None:
            raise JournalError(
                "Acquire controller ownership before accessing the journal."
            )
