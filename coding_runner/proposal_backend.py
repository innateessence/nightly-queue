"""Tool-free model process and bounded proposal contract shared by providers."""

import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from . import processes
from .approvals import ActionSpec
from .backends import (
    BackendError,
    BackendEvent,
    BackendInterrupted,
    BackendOutputError,
    BackendTimeout,
    Checkpoint,
)

MAX_BYTES = 4 * 1024 * 1024


class ProcessTransport:
    def __init__(self):
        self.process: subprocess.Popen | None = None
        self.lock = threading.Lock()
        self.interrupted = threading.Event()

    def command(self) -> list[str]:
        return [
            sys.executable,
            "-I",
            str(Path(__file__).with_name("model_worker.py").resolve()),
        ]

    def interrupt(self) -> None:
        with self.lock:
            if self.process is not None and self.process.poll() is None:
                self.interrupted.set()
                try:
                    os.killpg(self.process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass

    def exchange(
        self,
        payload: dict[str, Any],
        *,
        timeout: float,
        checkpoint: Checkpoint,
        stop: threading.Event,
    ) -> dict[str, Any]:
        raw = json.dumps(payload, allow_nan=False).encode()
        if len(raw) > MAX_BYTES:
            raise BackendOutputError("Model input exceeds the bounded request limit.")
        if stop.is_set() or checkpoint() is not True:
            raise BackendInterrupted("Model transport blocked at a control checkpoint.")
        started = time.monotonic()
        with tempfile.TemporaryFile() as incoming, tempfile.TemporaryFile() as outgoing:
            incoming.write(raw)
            incoming.seek(0)
            with self.lock:
                if self.process is not None:
                    raise BackendError("Another model request is active.")
                self.interrupted.clear()
                self.process = processes.popen(
                    self.command(),
                    cwd="/",
                    env={},
                    stdin=incoming,
                    stdout=outgoing,
                    stderr=subprocess.DEVNULL,
                    close_fds=True,
                    start_new_session=True,
                )
            process = self.process
            try:
                while process.poll() is None:
                    if (
                        self.interrupted.is_set()
                        or stop.is_set()
                        or checkpoint() is not True
                    ):
                        raise BackendInterrupted(
                            "Model request interrupted at a control checkpoint."
                        )
                    if time.monotonic() - started >= timeout:
                        raise BackendTimeout(
                            "Model request timed out; remote billing may be unresolved."
                        )
                    if outgoing.tell() > MAX_BYTES:
                        raise BackendOutputError(
                            "Model response exceeds the bounded output limit."
                        )
                    time.sleep(0.02)
                if (
                    self.interrupted.is_set()
                    or stop.is_set()
                    or checkpoint() is not True
                ):
                    raise BackendInterrupted(
                        "Model request interrupted at a control checkpoint."
                    )
                outgoing.seek(0)
                raw = outgoing.read(MAX_BYTES + 1)
                if process.returncode or len(raw) > MAX_BYTES:
                    raise BackendOutputError(
                        "Model transport did not return a valid response."
                    )
                try:
                    value = json.loads(raw)
                except (ValueError, UnicodeError, RecursionError):
                    raise BackendOutputError(
                        "Model transport did not return JSON."
                    ) from None
                if not isinstance(value, dict):
                    raise BackendOutputError(
                        "Model transport returned an invalid envelope."
                    )
                return value
            finally:
                if process.poll() is None:
                    try:
                        os.killpg(process.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                    try:
                        process.wait(timeout=0.2)
                    except subprocess.TimeoutExpired:
                        try:
                            os.killpg(process.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                process.wait()
                processes.reaped(process)
                with self.lock:
                    self.process = None


def parse_proposal(text: str, stage: str) -> BackendEvent:
    if stage not in {"research", "implementation"}:
        raise BackendOutputError("Unknown model proposal stage.")
    try:
        proposal = json.loads(text)
    except (ValueError, RecursionError):
        raise BackendOutputError("Model proposal is not JSON.") from None
    if (
        not isinstance(proposal, dict)
        or set(proposal) != {"kind", "data"}
        or not isinstance(proposal["kind"], str)
        or not isinstance(proposal["data"], dict)
    ):
        raise BackendOutputError("Model proposal envelope is invalid.")
    allowed = (
        {"read", "action", "plan"}
        if stage == "research"
        else {"read", "action", "patch", "complete", "amendment"}
    )
    if proposal["kind"] not in allowed:
        raise BackendOutputError("Model proposal is forbidden in this stage.")
    validate_proposal(proposal["kind"], proposal["data"])
    return BackendEvent(proposal["kind"], proposal["data"])


def safe_path(value: Any) -> bool:
    return (
        isinstance(value, str)
        and 0 < len(value) <= 2000
        and not value.startswith("/")
        and "\x00" not in value
        and not any(part in {"", ".", "..", ".git"} for part in value.split("/"))
    )


def sha(value: Any, size: str) -> bool:
    return isinstance(value, str) and re.fullmatch(size, value) is not None


def validate_model_data(value: Any) -> None:
    try:
        json.dumps(value, ensure_ascii=False, allow_nan=False).encode()
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise BackendOutputError(
            "Model proposal data is not finite UTF-8 JSON."
        ) from None
    pending = [(value, 0)]
    while pending:
        current, depth = pending.pop()
        if depth > 64:
            raise BackendOutputError("Model proposal exceeds its nesting limit.")
        children = current.values() if isinstance(current, dict) else current
        if not isinstance(current, (dict, list)):
            continue
        pending.extend(
            (entry, depth + 1) for entry in children if isinstance(entry, (dict, list))
        )


def validate_proposal(kind: str, data: dict[str, Any]) -> None:
    validate_model_data(data)
    valid = False
    if kind == "read":
        valid = set(data) == {"path"} and safe_path(data["path"])
    if kind == "action":
        if set(data) == {"action"}:
            try:
                ActionSpec.from_data(data["action"])
                valid = True
            except (ValueError, TypeError):
                pass
    if kind == "plan":
        valid = set(data) == {"document"} and isinstance(data["document"], dict)
    if kind == "amendment":
        valid = (
            set(data) == {"document", "reason"}
            and isinstance(data["document"], dict)
            and isinstance(data["reason"], str)
            and 0 < len(data["reason"].strip()) <= 20000
        )
    if kind == "complete":
        valid = (
            set(data) == {"summary"}
            and isinstance(data["summary"], str)
            and 0 < len(data["summary"]) <= 20000
        )
    if kind == "patch":
        valid = set(data) == {"base_revision", "content_digest", "files"}
        valid = (
            valid
            and sha(data["base_revision"], r"[0-9a-f]{40}(?:[0-9a-f]{24})?")
            and sha(data["content_digest"], r"[0-9a-f]{64}")
        )
        valid = (
            valid and isinstance(data["files"], list) and 1 <= len(data["files"]) <= 100
        )
        seen = set()
        for entry in data.get("files", []) if valid else []:
            if not isinstance(entry, dict) or set(entry) != {
                "path",
                "expected_digest",
                "content",
            }:
                valid = False
                break
            path = entry["path"]
            if not safe_path(path) or path in seen:
                valid = False
                break
            seen.add(path)
            expected, content = entry["expected_digest"], entry["content"]
            if expected is not None and not sha(expected, r"[0-9a-f]{64}"):
                valid = False
            if content is not None and (
                not isinstance(content, str) or len(content.encode()) > 128 * 1024
            ):
                valid = False
            if expected is None and content is None:
                valid = False
    if not valid:
        raise BackendOutputError(
            "Model proposal data does not match its bounded schema."
        )
