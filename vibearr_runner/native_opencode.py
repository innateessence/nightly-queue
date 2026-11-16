"""Pinned OpenCode sessions through the qualified OCI executor."""

import json
import re

from .native_adapter import NativeProcessAdapter
from .native_sessions import NativeSessionError

OPENCODE_VERSION = "1.19.5"
MAX_OUTPUT = 1024 * 1024
MAX_LINE = 256 * 1024
MAX_EVENTS = 2048


def opencode_profile(
    image, kind="connection", *, profile_id=None, limits=None, database_image=""
):
    from vibearr_executor.contracts import Limits, Profile
    from vibearr_executor.opencode_task import ARGV, HOSTS, SECRET_ENV

    if kind != "connection":
        raise NativeSessionError(
            "Native OpenCode requires an explicit OpenAI API-key connection."
        )
    return Profile(
        profile_id or "opencode-openai-v1",
        1,
        image,
        "agent",
        ARGV,
        limits or Limits(2, 4096, 128, 2048),
        HOSTS,
        SECRET_ENV,
        database_image,
    )


class OpenCodeEvents:
    def __init__(self):
        self.pending = b""
        self.received = 0
        self.events = []
        self.session_id = ""
        self.completed = False
        self.failed = False
        self.usage = {}
        self.parts = set()
        self.open_step = False

    def feed(self, content, *, final=False):
        self.received += len(content)
        if self.received > MAX_OUTPUT:
            raise NativeSessionError("Native OpenCode output exceeds its byte limit.")
        self.pending += content
        while b"\n" in self.pending:
            line, self.pending = self.pending.split(b"\n", 1)
            self.line(line)
        if len(self.pending) > MAX_LINE:
            raise NativeSessionError("Native OpenCode event exceeds its byte limit.")
        if final and self.pending.strip():
            try:
                json.loads(self.pending)
            except (ValueError, UnicodeError, RecursionError):
                raise NativeSessionError(
                    "Native OpenCode ended with an incomplete event."
                ) from None
            self.line(self.pending)
            self.pending = b""
        return self.events

    def line(self, line):
        if len(line) > MAX_LINE or len(self.events) >= MAX_EVENTS:
            raise NativeSessionError("Native OpenCode event limit exceeded.")
        if not line.strip():
            return
        try:
            value = json.loads(line)
        except (ValueError, UnicodeError, RecursionError):
            self.events.append(
                {
                    "kind": "diagnostic",
                    "text": "OpenCode emitted a non-event diagnostic.",
                }
            )
            return
        if not isinstance(value, dict):
            raise NativeSessionError("Native OpenCode event must be an object.")
        kind = value.get("type")
        if kind == "error":
            if (
                value.get("sessionID")
                and self.session_id
                and value["sessionID"] != self.session_id
            ):
                raise NativeSessionError(
                    "Native OpenCode error belongs to another session."
                )
            self.failed = True
            self.events.append(
                {
                    "kind": "error",
                    "text": "Native OpenCode reported an execution, model or authentication error.",
                }
            )
            return
        if kind not in {"step_start", "step_finish", "tool_use", "text", "reasoning"}:
            self.events.append(
                {
                    "kind": "diagnostic",
                    "text": "OpenCode emitted an unrecognized event.",
                }
            )
            return
        session_id = value.get("sessionID")
        if (
            not isinstance(session_id, str)
            or re.fullmatch(r"ses_[A-Za-z0-9_-]{1,180}", session_id) is None
        ):
            raise NativeSessionError("Native OpenCode session identity is invalid.")
        if self.session_id and self.session_id != session_id:
            raise NativeSessionError("Native OpenCode session identity changed.")
        if not self.session_id:
            self.session_id = session_id
            self.events.append({"kind": "session.started", "session_id": session_id})
        part = value.get("part")
        expected_type = "tool" if kind == "tool_use" else kind.replace("_", "-")
        if (
            not isinstance(part, dict)
            or part.get("sessionID") != session_id
            or part.get("type") != expected_type
        ):
            raise NativeSessionError("Native OpenCode event part is invalid.")
        part_id = part.get("id")
        if not isinstance(part_id, str) or not part_id or part_id in self.parts:
            raise NativeSessionError(
                "Native OpenCode event part is missing or duplicated."
            )
        self.parts.add(part_id)
        if self.completed:
            raise NativeSessionError(
                "Native OpenCode emitted an event after completion."
            )
        if kind == "step_start":
            if self.open_step:
                raise NativeSessionError("Native OpenCode step is already active.")
            self.open_step = True
            self.events.append({"kind": "turn.started"})
            return
        if kind == "step_finish":
            self.finish(part)
            return
        if kind in {"text", "reasoning"}:
            if not isinstance(part.get("text"), str):
                raise NativeSessionError("Native OpenCode text is invalid.")
            self.events.append(
                {
                    "kind": "item.completed",
                    "item_type": kind,
                    "text": part["text"][:4000],
                }
            )
            return
        state = part.get("state")
        if not isinstance(state, dict) or state.get("status") not in {
            "completed",
            "error",
        }:
            raise NativeSessionError("Native OpenCode tool completion is invalid.")
        self.events.append(
            {
                "kind": "item.completed",
                "item_type": "tool",
                "tool": str(part.get("tool", ""))[:80],
                "successful": state["status"] == "completed",
            }
        )

    def finish(self, part):
        if not self.open_step:
            raise NativeSessionError("Native OpenCode completed a missing step.")
        self.open_step = False
        tokens = part.get("tokens")
        if not isinstance(tokens, dict) or not isinstance(tokens.get("cache"), dict):
            raise NativeSessionError("Native OpenCode token usage is invalid.")
        fields = {
            "input": tokens.get("input"),
            "output": tokens.get("output"),
            "reasoning": tokens.get("reasoning"),
            "cached_input": tokens["cache"].get("read"),
            "cache_write": tokens["cache"].get("write"),
        }
        for key, amount in fields.items():
            if type(amount) is not int or not 0 <= amount < 2**63:
                raise NativeSessionError("Native OpenCode token usage is invalid.")
            total = self.usage.get(key, 0) + amount
            if total >= 2**63:
                raise NativeSessionError(
                    "Native OpenCode token usage exceeds its limit."
                )
            self.usage[key] = total
        reason = part.get("reason")
        if reason == "stop":
            self.completed = True
            self.events.append(
                {"kind": "turn.completed", "usage": dict(self.usage), "unit": "tokens"}
            )
            return
        if reason != "tool-calls":
            self.failed = True
            self.events.append(
                {
                    "kind": "error",
                    "text": "Native OpenCode did not finish its response successfully.",
                }
            )


class NativeOpenCodeAdapter(NativeProcessAdapter):
    provider = "opencode"
    version = OPENCODE_VERSION
    profile_factory = staticmethod(opencode_profile)
    events_factory = staticmethod(OpenCodeEvents)

    def task_api(self):
        from vibearr_executor.opencode_task import auth_material, context_data

        return auth_material, context_data

    def diagnostics(self, policy, credential, *, model=None, proof=None):
        selected = (
            isinstance(model, str)
            and re.fullmatch(r"openai/[A-Za-z0-9][A-Za-z0-9._:-]{0,191}", model)
            is not None
        )
        if model is not None and not selected:
            return {
                "native_adapter_available": True,
                "auth_available": False,
                "authenticated": False,
                "native_agent_ready": False,
                "reason": "Select an explicit openai/model for the native OpenCode connection.",
            }
        return super().diagnostics(policy, credential, model=model, proof=proof)
