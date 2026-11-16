import datetime
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.core.exceptions import ValidationError


BACKENDS = ["codex", "claude", "opencode"]
STAGE_FIELDS = ("backend", "model", "instructions", "credential_id")
OVERRIDE_FIELDS = tuple(
    f"{stage}_{field}"
    for stage in ("research", "implementation")
    for field in STAGE_FIELDS
)
OVERRIDE_FIELDS += (
    "setup_commands",
    "validation_commands",
    "issue_credential_id",
    "repository_credential_id",
)


def validate_timezone(value):
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValidationError(
            "Enter an IANA timezone, such as America/Los_Angeles."
        ) from None


def validate_windows(value):
    if not isinstance(value, list) or len(value) > 32:
        raise ValidationError("Enter a list of at most 32 windows.")
    for window in value:
        if not isinstance(window, dict) or set(window) != {"days", "start", "end"}:
            raise ValidationError("Each window needs days, start and end.")
        days = window["days"]
        if (
            not isinstance(days, list)
            or not days
            or any(type(day) is not int or day not in range(7) for day in days)
        ):
            raise ValidationError(
                "Days must contain weekday numbers 0 (Monday) through 6 (Sunday)."
            )
        for key in ("start", "end"):
            text = window[key]
            if not isinstance(text, str) or len(text) != 5:
                raise ValidationError("Window times must use HH:MM.")
            try:
                datetime.datetime.strptime(text, "%H:%M")
            except ValueError:
                raise ValidationError("Window times must use HH:MM.") from None
        if window["start"] == window["end"]:
            raise ValidationError(
                "Window start and end must differ; use continuous for all day."
            )


def validate_commands(value):
    if not isinstance(value, list) or len(value) > 100:
        raise ValidationError("Enter a list of at most 100 commands.")
    if any(
        not isinstance(command, str) or not command.strip() or len(command) > 4000
        for command in value
    ):
        raise ValidationError(
            "Commands must be nonempty strings of at most 4000 characters."
        )
    for command in value:
        validate_json_text(command)


def validate_json_text(value):
    if "\x00" in value:
        raise ValidationError("Text cannot contain null characters.")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise ValidationError("Enter valid Unicode text.") from None


def validate_overrides(value):
    if not isinstance(value, dict) or set(value) - set(OVERRIDE_FIELDS):
        raise ValidationError("Enter supported project override fields.")
    for key, entry in value.items():
        if not isinstance(entry, dict) or entry.get("mode") not in (
            "inherit",
            "replace",
            "clear",
        ):
            raise ValidationError(
                "Each override needs an inherit, replace or clear mode."
            )
        if set(entry) - {"mode", "value"}:
            raise ValidationError("Unknown override property.")
        if entry["mode"] != "replace":
            continue
        replacement = entry.get("value")
        if key.endswith("commands"):
            validate_commands(replacement)
        elif key.endswith("credential_id"):
            if type(replacement) is not int or replacement < 1:
                raise ValidationError("Choose a saved credential.")
        elif (
            not isinstance(replacement, str)
            or not replacement.strip()
            or len(replacement) > 20000
        ):
            raise ValidationError(
                "Replacement text must be nonempty and at most 20000 characters."
            )
        elif key.endswith("backend") and replacement not in BACKENDS:
            raise ValidationError("Choose a supported backend.")
        else:
            validate_json_text(replacement)


def validate_backend_model(backend, model):
    if backend != "claude" or not model:
        return
    if (
        len(model) > 200
        or re.fullmatch(r"claude-[a-z0-9]+(?:-[a-z0-9]+)*", model) is None
    ):
        raise ValidationError(
            "Enter a Claude API model ID, such as claude-sonnet-5-5; endpoint URLs and CLI aliases are unsupported."
        )
