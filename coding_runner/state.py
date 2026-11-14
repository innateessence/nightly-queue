import fcntl
import json
import os
from contextlib import contextmanager


class Journal:
    def __init__(self, root):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.active = root / "active.json"

    @contextmanager
    def locked(self):
        with (self.root / "runner.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError(
                    "Another runner is using this data volume."
                ) from None
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def read(self):
        if not self.active.exists():
            return None
        return json.loads(self.active.read_text())

    def write(self, state):
        self.atomic_write(self.active, state)

    def run_dir(self, run_id):
        path = self.root / "runs" / run_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def archive(self, state):
        path = self.run_dir(state["run_id"]) / "state.json"
        self.atomic_write(path, state)
        self.active.unlink()
        self.sync_directory(self.root)

    @staticmethod
    def sync_directory(path):
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @classmethod
    def atomic_write(cls, path, data):
        temporary = path.with_suffix(".tmp")
        with temporary.open("w") as target:
            os.chmod(temporary, 0o600)
            json.dump(data, target)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, path)
        cls.sync_directory(path.parent)
