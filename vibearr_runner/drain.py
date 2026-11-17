"""Drain new claims while owned work and recovery continue."""

import argparse
import fcntl
import json
import os
from pathlib import Path
import tempfile
import time
from uuid import UUID, uuid4

from .bootstrap_files import private_json
from .environment import getenv
from .journal import Journal


def requested(directory):
    path = directory / "drain.json"
    if not path.exists():
        return None
    value = private_json(path, 4096)
    if (
        set(value) != {"schema_version", "request_id"}
        or type(value["schema_version"]) is not int
        or value["schema_version"] != 1
    ):
        raise ValueError("Unsupported private drain request.")
    if str(UUID(value["request_id"])) != value["request_id"]:
        raise ValueError("Invalid drain request identity.")
    return value["request_id"]


def acknowledge(journal, state):
    identity = requested(journal.directory)
    if identity is None:
        return False
    pending = (
        state.get("run") or state.get("operation") or state.get("remote_active_run_id")
    )
    pending = pending or state.get("gates", {}).get("pending_claim")
    if not pending and state.get("drain_ack") != identity:
        state["drain_ack"] = identity
        journal.save(state)
    return True


def sync(directory):
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def change(directory, enabled):
    Journal(directory)
    descriptor = os.open(
        directory / "drain-control.lock",
        os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW,
        0o600,
    )
    with os.fdopen(descriptor, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        path = directory / "drain.json"
        if not enabled:
            path.unlink(missing_ok=True)
            sync(directory)
            return
        if requested(directory) is not None:
            return
        value = {"schema_version": 1, "request_id": str(uuid4())}
        descriptor, temporary = tempfile.mkstemp(prefix=".drain-", dir=directory)
        try:
            with os.fdopen(descriptor, "w") as stream:
                json.dump(value, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            sync(directory)
        finally:
            Path(temporary).unlink(missing_ok=True)


def status(directory):
    Journal(directory)
    identity = requested(directory)
    path = directory / "journal.json"
    state = private_json(path) if path.exists() else {}
    idle = not (
        state.get("run") or state.get("operation") or state.get("remote_active_run_id")
    )
    idle = idle and not state.get("gates", {}).get("pending_claim")
    return {
        "requested": identity is not None,
        "request_id": identity,
        "drained": bool(identity and idle and state.get("drain_ack") == identity),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("on", "off", "status", "wait"))
    parser.add_argument("--timeout", type=int, default=300)
    options = parser.parse_args()
    directory = Path(getenv("NQ_RUNNER_STATE_DIR", "/var/lib/vibearr"))
    if not 1 <= options.timeout <= 3600:
        parser.error("Drain timeout must be between one second and one hour.")
    if options.action in {"on", "off"}:
        change(directory, options.action == "on")
    if options.action == "wait":
        deadline = time.monotonic() + options.timeout
        while not status(directory)["drained"]:
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    "The controller has not acknowledged this drain generation."
                )
            time.sleep(0.2)
    print(json.dumps(status(directory)))


if __name__ == "__main__":
    main()
