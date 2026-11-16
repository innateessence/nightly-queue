"""Pinned Claude Code sessions, independent of the Messages proposal transport."""

import json
from uuid import UUID

from .native_adapter import NativeProcessAdapter
from .native_sessions import NativeSessionError

CLAUDE_VERSION = "2.1.325"
MAX_EVENTS = 2048
MAX_OUTPUT = 1024 * 1024
MAX_LINE = 256 * 1024
TOOLS = {"Bash", "Read", "Edit", "Write", "Glob", "Grep", "EndConversation"}
BUILTIN_PLUGINS = {
    "cc-plugin-agents-md",
    "cc-plugin-telemetry",
    "cc-plugin-privacy",
    "cc-plugin-plugin-authoring",
}


def claude_profile(
    image: str, kind: str, *, profile_id=None, limits=None, database_image=""
):
    from vibearr_executor.claude_task import ARGV, HOSTS, SECRET_ENV
    from vibearr_executor.contracts import Limits, Profile

    if kind not in HOSTS:
        raise NativeSessionError("Native Claude supports a dedicated API key only.")
    return Profile(
        profile_id or f"claude-{kind}-v1",
        1,
        image,
        "agent",
        ARGV,
        limits or Limits(2, 4096, 128, 2048),
        HOSTS[kind],
        SECRET_ENV,
        database_image,
    )


class ClaudeEvents:
    def __init__(self):
        self.pending = b""
        self.received = 0
        self.events = []
        self.session_id = ""
        self.completed = False
        self.failed = False
        self.usage = {}
        self.initialization = None

    def feed(self, content: bytes, *, final=False):
        self.received += len(content)
        if self.received > MAX_OUTPUT:
            raise NativeSessionError("Native Claude output exceeds its byte limit.")
        self.pending += content
        while b"\n" in self.pending:
            line, self.pending = self.pending.split(b"\n", 1)
            self.line(line)
        if len(self.pending) > MAX_LINE:
            raise NativeSessionError("Native Claude event exceeds its byte limit.")
        if final and self.pending.strip():
            self.line(self.pending)
            self.pending = b""
        return self.events

    def identity(self, value):
        session_id = value.get("session_id")
        try:
            if (
                str(UUID(session_id)) != session_id
                or self.session_id
                and session_id != self.session_id
            ):
                raise ValueError
        except (ValueError, TypeError, AttributeError):
            raise NativeSessionError(
                "Native Claude session identity is invalid or changed."
            ) from None
        self.session_id = session_id

    def line(self, line: bytes):
        if not line.strip():
            return
        if len(line) > MAX_LINE or len(self.events) >= MAX_EVENTS:
            raise NativeSessionError("Native Claude event limit exceeded.")
        try:
            value = json.loads(line)
        except (ValueError, UnicodeError, RecursionError):
            raise NativeSessionError(
                "Native Claude emitted malformed event output."
            ) from None
        if not isinstance(value, dict):
            raise NativeSessionError("Native Claude event must be an object.")
        kind = value.get("type")
        if self.completed:
            raise NativeSessionError(
                "Native Claude emitted output after its terminal result."
            )
        if kind == "error":
            self.failed = True
            self.events.append(
                {
                    "kind": "error",
                    "text": "Native Claude configuration or authentication failed.",
                }
            )
            return
        self.identity(value)
        if kind == "system" and value.get("subtype") == "init":
            if self.initialization is not None:
                raise NativeSessionError(
                    "Native Claude emitted duplicate initialization."
                )
            self.initialization = value
            self.events.append(
                {"kind": "session.started", "session_id": self.session_id}
            )
        elif kind == "result":
            self.result(value)
        elif kind == "assistant":
            self.assistant(value)
        elif kind == "user":
            self.events.append({"kind": "tool.result"})
        elif kind == "system" and value.get("subtype") == "permission_denied":
            self.failed = True
            self.events.append(
                {"kind": "error", "text": "Claude tool permission was denied."}
            )
        else:
            self.events.append(
                {
                    "kind": "diagnostic",
                    "text": "Claude emitted a nonterminal diagnostic.",
                }
            )

    def result(self, value):
        if type(value.get("is_error")) is not bool or not isinstance(
            value.get("permission_denials"), list
        ):
            raise NativeSessionError("Native Claude terminal result is invalid.")
        self.failed = (
            self.failed
            or value.get("subtype") != "success"
            or value["is_error"]
            or bool(value["permission_denials"])
        )
        usage = value.get("usage")
        if not isinstance(usage, dict):
            raise NativeSessionError("Native Claude usage is invalid.")
        for key in (
            "input_tokens",
            "output_tokens",
            "cache_read_input_tokens",
            "cache_creation_input_tokens",
        ):
            if key not in usage:
                continue
            if type(usage[key]) is not int or not 0 <= usage[key] < 2**63:
                raise NativeSessionError(
                    "Native Claude usage requires nonnegative token counts."
                )
            self.usage[key] = usage[key]
        self.completed = True
        self.events.append(
            {
                "kind": "turn.failed" if self.failed else "turn.completed",
                "usage": dict(self.usage),
                "unit": "tokens",
            }
        )

    def assistant(self, value):
        if value.get("error") or value.get("is_api_error_message"):
            self.failed = True
        message = value.get("message")
        if not isinstance(message, dict) or not isinstance(
            message.get("content"), list
        ):
            raise NativeSessionError("Native Claude assistant message is invalid.")
        for block in message["content"]:
            if not isinstance(block, dict):
                raise NativeSessionError("Native Claude message block is invalid.")
            if block.get("type") == "text" and isinstance(block.get("text"), str):
                self.events.append(
                    {
                        "kind": "item.completed",
                        "item_type": "agent_message",
                        "text": block["text"][:4000],
                    }
                )
            elif block.get("type") == "tool_use":
                name = block.get("name")
                if not isinstance(name, str) or name not in TOOLS:
                    raise NativeSessionError("Native Claude used an unconfigured tool.")
                event = {
                    "kind": "item.started",
                    "item_type": (
                        "command_execution" if name == "Bash" else "file_operation"
                    ),
                    "tool": name,
                }
                arguments = block.get("input")
                if (
                    name == "Bash"
                    and isinstance(arguments, dict)
                    and isinstance(arguments.get("command"), str)
                ):
                    event["command"] = arguments["command"][:4000]
                self.events.append(event)
            else:
                self.events.append(
                    {
                        "kind": "diagnostic",
                        "text": "Claude emitted an auxiliary message block.",
                    }
                )
        if len(self.events) > MAX_EVENTS:
            raise NativeSessionError("Native Claude event limit exceeded.")

    def validate(self, session):
        value = self.initialization
        if value is None:
            if self.completed and not self.failed:
                raise NativeSessionError(
                    "Native Claude successful result has no initialization."
                )
            return
        tools = value.get("tools")
        plugins = value.get("plugins")
        clean_plugins = isinstance(plugins, list) and all(
            isinstance(plugin, dict)
            and isinstance(plugin.get("name"), str)
            and plugin.get("name") in BUILTIN_PLUGINS
            and plugin.get("path") == "builtin"
            and plugin.get("source") == plugin["name"] + "@builtin"
            for plugin in plugins
        )
        if (
            value.get("claude_code_version") != CLAUDE_VERSION
            or value.get("model") != session.account["model"]
            or value.get("permissionMode") != "dontAsk"
            or value.get("cwd") != "/work"
            or not isinstance(tools, list)
            or not all(isinstance(name, str) and name in TOOLS for name in tools)
            or value.get("mcp_servers") != []
            or not clean_plugins
            or value.get("skills") != []
            or value.get("slash_commands") != []
        ):
            raise NativeSessionError(
                "Native Claude initialization does not match the frozen model, tools and clean configuration."
            )


class NativeClaudeAdapter(NativeProcessAdapter):
    provider = "claude"
    version = CLAUDE_VERSION
    profile_factory = staticmethod(claude_profile)
    events_factory = staticmethod(ClaudeEvents)

    def task_api(self):
        from vibearr_executor.claude_task import auth_material, context_data

        return auth_material, context_data

    def validate_events(self, parser, session):
        parser.validate(session)
