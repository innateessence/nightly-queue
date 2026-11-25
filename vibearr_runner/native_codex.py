"""Bounded Codex event parsing and the fixed native authentication probe."""

import json
from uuid import UUID

from .native_sessions import NativeSessionError

CODEX_VERSION = "0.169.0"
MAX_EVENTS = 2048
MAX_OUTPUT = 1024 * 1024
MAX_LINE = MAX_OUTPUT
AUTH_PROMPT = "Authentication diagnostic: reply with a short greeting only. Do not run tools, edit files, or access another repository."


def auth_source():
    from vibearr_executor.artifacts import pack

    return pack(
        {
            "vibearr-auth-probe.txt": (
                0o644,
                b"Trusted native authentication diagnostic.\n",
            )
        }
    )


class CodexEvents:
    def __init__(self):
        self.pending = b""
        self.received = 0
        self.events = []
        self.session_id = ""
        self.completed = False
        self.failed = False
        self.usage = {}

    def feed(self, content: bytes, *, final=False):
        self.received += len(content)
        if self.received > MAX_OUTPUT:
            raise NativeSessionError("Native Codex output exceeds its byte limit.")
        self.pending += content
        while b"\n" in self.pending:
            line, self.pending = self.pending.split(b"\n", 1)
            self.line(line)
        if len(self.pending) > MAX_LINE:
            raise NativeSessionError("Native Codex event exceeds its byte limit.")
        if final and self.pending.strip():
            self.line(self.pending)
            self.pending = b""
        return self.events

    def line(self, line: bytes):
        if len(line) > MAX_LINE:
            raise NativeSessionError("Native Codex event exceeds its byte limit.")
        if len(self.events) >= MAX_EVENTS:
            raise NativeSessionError("Native Codex event count exceeds its limit.")
        if not line.strip():
            return
        try:
            value = json.loads(line)
        except (ValueError, UnicodeError, RecursionError):
            self.events.append(
                {"kind": "diagnostic", "text": "Codex emitted a non-event diagnostic."}
            )
            return
        if not isinstance(value, dict):
            raise NativeSessionError("Native Codex event must be an object.")
        kind = value.get("type")
        if kind == "thread.started":
            session_id = value.get("thread_id")
            try:
                if (
                    str(UUID(session_id)) != session_id
                    or self.session_id
                    and session_id != self.session_id
                ):
                    raise ValueError
            except (ValueError, TypeError, AttributeError):
                raise NativeSessionError(
                    "Native Codex session identity is invalid or changed."
                ) from None
            self.session_id = session_id
            self.events.append({"kind": "session.started", "session_id": session_id})
        elif kind == "turn.started":
            self.events.append({"kind": "turn.started"})
        elif kind == "turn.completed":
            if self.completed:
                raise NativeSessionError(
                    "Native Codex emitted duplicate turn completion."
                )
            usage = value.get("usage", {})
            if not isinstance(usage, dict):
                raise NativeSessionError("Native Codex usage is invalid.")
            for key in (
                "input_tokens",
                "cached_input_tokens",
                "output_tokens",
                "reasoning_output_tokens",
            ):
                if key not in usage:
                    continue
                if type(usage[key]) is not int or not 0 <= usage[key] < 2**63:
                    raise NativeSessionError("Native Codex usage is invalid.")
                self.usage[key] = usage[key]
            self.completed = True
            self.events.append(
                {"kind": "turn.completed", "usage": dict(self.usage), "unit": "tokens"}
            )
        elif kind in {"turn.failed", "error"}:
            self.failed = self.failed or kind == "turn.failed"
            self.events.append(
                {
                    "kind": "error" if kind == "turn.failed" else "diagnostic",
                    "text": "Native Codex reported an execution or authentication error.",
                }
            )
        elif kind in {"item.started", "item.updated", "item.completed"}:
            item = value.get("item")
            if not isinstance(item, dict):
                raise NativeSessionError("Native Codex item is invalid.")
            event = {"kind": kind, "item_type": str(item.get("type", "unknown"))[:80]}
            if item.get("type") == "agent_message" and isinstance(
                item.get("text"), str
            ):
                event["text"] = item["text"][:4000]
            if item.get("type") == "command_execution" and isinstance(
                item.get("command"), str
            ):
                event["command"] = item["command"][:4000]
            self.events.append(event)
        else:
            self.events.append(
                {"kind": "diagnostic", "text": "Codex emitted an unrecognized event."}
            )
