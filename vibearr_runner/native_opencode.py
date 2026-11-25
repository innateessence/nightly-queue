"""Strict event handling for the pinned phase-local OpenCode CLI."""

import json
import math
import re

from .native_codex import MAX_EVENTS, MAX_OUTPUT
from .native_sessions import NativeSessionError


def unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Duplicate JSON key.")
        value[key] = item
    return value


def invalid_constant(_value):
    raise ValueError("Nonfinite JSON value.")


def finite_number(value):
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("Nonfinite JSON number.")
    return number


def connection(secret):
    from vibearr_executor.ollama_relay import external_endpoint

    if not isinstance(secret, str) or len(secret.encode()) > 65536:
        raise NativeSessionError("OpenCode connection exceeds its input limit.")
    try:
        value = json.loads(
            secret,
            object_pairs_hook=unique_object,
            parse_constant=invalid_constant,
            parse_float=finite_number,
        )
    except (ValueError, UnicodeError, RecursionError):
        raise NativeSessionError("OpenCode connection is malformed.") from None
    if (
        not isinstance(value, dict)
        or set(value) != {"provider", "base_url"}
        or value.get("provider") != "ollama"
    ):
        raise NativeSessionError("OpenCode requires only its selected external Ollama.")
    return external_endpoint(value.get("base_url"))


def usage(value):
    if not isinstance(value, dict):
        raise NativeSessionError("OpenCode token usage must be an object.")
    result = {}
    for key, item in value.items():
        if key == "cache":
            if not isinstance(item, dict) or set(item) - {"read", "write"}:
                raise NativeSessionError("OpenCode cache token usage is invalid.")
            for name, count in item.items():
                if type(count) is not int or not 0 <= count < 2**63:
                    raise NativeSessionError("OpenCode token count is invalid.")
                result["cache_" + name] = count
            continue
        if key not in {"input", "output", "reasoning", "total"}:
            raise NativeSessionError("OpenCode token usage contains an unknown field.")
        if type(item) is not int or not 0 <= item < 2**63:
            raise NativeSessionError("OpenCode token count is invalid.")
        result[key] = item
    return result


def consume(parser, line):
    if not line.strip():
        return
    if len(line) > MAX_OUTPUT or len(parser.events) >= MAX_EVENTS:
        raise NativeSessionError("OpenCode event output exceeds its limit.")
    try:
        event = json.loads(
            line,
            object_pairs_hook=unique_object,
            parse_constant=invalid_constant,
            parse_float=finite_number,
        )
    except (ValueError, UnicodeError, RecursionError):
        raise NativeSessionError("OpenCode emitted malformed JSON output.") from None
    if not isinstance(event, dict):
        raise NativeSessionError("OpenCode events must be objects.")
    if "is_error" in event and type(event["is_error"]) is not bool:
        raise NativeSessionError("OpenCode error flag must be a boolean.")
    identity = event.get("sessionID")
    if (
        not isinstance(identity, str)
        or len(identity) > 200
        or not re.fullmatch(r"ses_[A-Za-z0-9_-]+", identity)
        or parser.session_id
        and parser.session_id != identity
    ):
        raise NativeSessionError("OpenCode session identity is invalid or changed.")
    kind = event.get("type")
    if not isinstance(kind, str) or not 0 < len(kind) <= 80:
        raise NativeSessionError("OpenCode event type is invalid.")
    if parser.completed or parser.failed:
        raise NativeSessionError("OpenCode emitted events after its terminal result.")
    if not parser.session_id:
        parser.events.append({"kind": "session.started", "session_id": identity})
    parser.session_id = identity
    part = event.get("part", {})
    if not isinstance(part, dict):
        raise NativeSessionError("OpenCode event part must be an object.")
    if "sessionID" in part and part["sessionID"] != identity:
        raise NativeSessionError("OpenCode event part belongs to another session.")
    normalized = {"kind": kind, "session_id": identity}
    if kind == "error" or event.get("is_error") is True:
        parser.failed = True
        parser.failure_reason = parser.classify_failure(event)
        normalized["text"] = parser.failure_reason
    elif kind == "step_start":
        parser.result_text = ""
    elif kind == "text":
        text = part.get("text")
        if not isinstance(text, str):
            raise NativeSessionError("OpenCode text must be a string.")
        previous = getattr(parser, "result_text", "")
        if len((previous + text).encode()) > MAX_OUTPUT:
            raise NativeSessionError("OpenCode role result exceeds its output limit.")
        parser.result_text = previous + text
        normalized["text"] = text[:4000]
    elif kind == "step_finish":
        reason = part.get("reason")
        if not isinstance(reason, str) or not 0 < len(reason) <= 80:
            raise NativeSessionError("OpenCode finish reason is invalid.")
        counts = usage(part.get("tokens", {}))
        part_id = part.get("id")
        if part_id is not None:
            if not isinstance(part_id, str) or not 0 < len(part_id) <= 200:
                raise NativeSessionError("OpenCode step identity is invalid.")
            seen = getattr(parser, "_opencode_finish_ids", set())
            if part_id in seen:
                raise NativeSessionError("OpenCode emitted a duplicate step result.")
            seen.add(part_id)
            parser._opencode_finish_ids = seen
        for key, count in counts.items():
            total = parser.usage.get(key, 0) + count
            if total >= 2**63:
                raise NativeSessionError(
                    "OpenCode aggregate token count exceeds its limit."
                )
            parser.usage[key] = total
        if reason == "stop":
            parser.completed = True
        elif reason != "tool-calls":
            parser.failed = True
            parser.failure_reason = (
                "OpenCode stopped without completing the selected task."
            )
            normalized["text"] = parser.failure_reason
    parser.events.append(normalized)
