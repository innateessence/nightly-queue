import hashlib
import json
import os
from pathlib import Path
from uuid import UUID

from .workspaces import private_directory


class RunLogs:
    def __init__(self, directory: Path, publish=None):
        self.directory = private_directory(directory)
        self.publish = publish

    def __call__(self, workspace, action, result):
        run_id, execution_id = str(UUID(workspace.workspace_id)), str(
            UUID(result.execution_id)
        )
        directory = private_directory(self.directory / run_id)
        payload = result.output[:1048576]
        digest = hashlib.sha256(payload).hexdigest()
        reference = f"{run_id}/{execution_id}"
        metadata = {
            "log_reference": reference,
            "execution_id": execution_id,
            "exit_code": result.exit_code,
            "interrupted": result.interrupted,
            "output_digest": digest,
            "output_bytes": len(payload),
            "base_revision": workspace.base_revision,
        }
        self.write(directory / (execution_id + ".log"), payload)
        self.write(
            directory / (execution_id + ".json"),
            json.dumps(
                {**metadata, "action": action.to_data()}, allow_nan=False
            ).encode(),
        )
        if self.publish:
            self.publish(metadata)

    @staticmethod
    def write(path: Path, content: bytes):
        try:
            descriptor = os.open(
                path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600
            )
        except FileExistsError:
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(descriptor, "rb") as stream:
                if (
                    hashlib.sha256(stream.read()).digest()
                    != hashlib.sha256(content).digest()
                ):
                    raise ValueError(
                        "Execution log identity has conflicting content."
                    ) from None
            return
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
