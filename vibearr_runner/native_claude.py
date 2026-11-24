"""Validate the pinned Claude stream without treating display text as a result."""

import json
import math

from .native_codex import MAX_EVENTS, MAX_LINE
from .native_sessions import NativeSessionError


TOKEN_FIELDS = (
    "input_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
    "output_tokens",
)


def unique_fields(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Duplicate Claude event field.")
        value[key] = item
    return value


def reject_constant(_value):
    raise ValueError("Nonfinite Claude event value.")


def finite_number(value):
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("Nonfinite Claude event value.")
    return number


def append_event(parser, value):
    if len(parser.events) >= MAX_EVENTS:
        raise NativeSessionError("Native Claude event count exceeds its limit.")
    parser.events.append(value)


def validate_usage(value):
    if not isinstance(value, dict):
        raise NativeSessionError("Native Claude usage must be an object.")
    usage = {}
    for key in TOKEN_FIELDS:
        if key not in value:
            continue
        count = value[key]
        if type(count) is not int or not 0 <= count < 2**63:
            raise NativeSessionError("Native Claude token usage is invalid.")
        usage[key] = count
    return usage


def consume_claude(parser, line):
    if len(line) > MAX_LINE:
        raise NativeSessionError("Native Claude event exceeds its byte limit.")
    if not line.strip():
        return
    if getattr(parser, "_claude_terminal", False):
        raise NativeSessionError(
            "Native Claude emitted output after its terminal result."
        )
    try:
        event = json.loads(
            line,
            object_pairs_hook=unique_fields,
            parse_constant=reject_constant,
            parse_float=finite_number,
        )
    except (ValueError, UnicodeError, RecursionError):
        raise NativeSessionError(
            "Native Claude emitted malformed event JSON."
        ) from None
    if not isinstance(event, dict):
        raise NativeSessionError("Native Claude events must be objects.")
    kind = event.get("type")
    if not isinstance(kind, str) or not kind or len(kind) > 80:
        raise NativeSessionError("Native Claude event type is invalid.")
    identity = event.get("session_id")
    if "session_id" in event:
        if (
            not isinstance(identity, str)
            or not identity
            or len(identity) > 200
            or any(character.isspace() or ord(character) < 32 for character in identity)
            or parser.session_id
            and parser.session_id != identity
        ):
            raise NativeSessionError(
                "Native Claude session identity is invalid or changed."
            )
        if not parser.session_id:
            append_event(parser, {"kind": "session.started", "session_id": identity})
        parser.session_id = identity
    normalized = {"kind": kind, "session_id": parser.session_id}
    if "is_error" in event and type(event["is_error"]) is not bool:
        raise NativeSessionError("Native Claude error flag is invalid.")
    if kind == "error" or event.get("is_error") is True:
        parser.failed = True
        parser.failure_reason = parser.classify_failure(event)
    if kind == "result":
        subtype = event.get("subtype")
        error = event.get("is_error")
        if not isinstance(subtype, str) or type(error) is not bool:
            raise NativeSessionError("Native Claude terminal result is invalid.")
        successful = subtype == "success"
        if successful == error or not successful and not subtype.startswith("error_"):
            raise NativeSessionError("Native Claude terminal outcome is contradictory.")
        if successful and (not parser.session_id or "session_id" not in event):
            raise NativeSessionError(
                "Native Claude completion has no session identity."
            )
        result = event.get("result")
        if successful and (not isinstance(result, str) or not result.strip()):
            raise NativeSessionError("Native Claude completion has no result text.")
        if result is not None and not isinstance(result, str):
            raise NativeSessionError("Native Claude result text is invalid.")
        parser.usage = validate_usage(event.get("usage", {}))
        parser.complete_result = result
        parser._claude_terminal = True
        parser.completed = successful and not parser.failed
        parser.failed = parser.failed or not successful
        if parser.failed:
            parser.failure_reason = parser.classify_failure(event)
    if parser.failure_reason:
        normalized["text"] = parser.failure_reason
    elif kind == "assistant":
        message = event.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list) or any(
            not isinstance(item, dict) for item in content
        ):
            raise NativeSessionError("Native Claude assistant content is invalid.")
        text = []
        for item in content:
            if item.get("type") != "text":
                continue
            if not isinstance(item.get("text"), str):
                raise NativeSessionError("Native Claude assistant text is invalid.")
            text.append(item["text"])
        normalized["text"] = " ".join(text)[:4000]
    append_event(parser, normalized)
