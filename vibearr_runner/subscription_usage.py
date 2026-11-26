"""Read subscription allowances without starting a model or exposing login material."""

from datetime import UTC, datetime
import json
import math
import urllib.request

from .gates import UsageReading, UsageWindow


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("Usage endpoint redirects are not supported.")


def percentage(value):
    if (
        type(value) not in (int, float)
        or not math.isfinite(value)
        or not 0 <= value <= 100
    ):
        raise ValueError("Invalid subscription usage percentage.")
    return 100 - value


def codex_windows(payload, now):
    limits = payload["rate_limit"]
    if not isinstance(limits, dict):
        raise ValueError("Subscription usage windows are missing.")
    windows = []
    for field in ("primary_window", "secondary_window"):
        window = limits.get(field)
        if window is None:
            continue
        duration = window["limit_window_seconds"]
        if type(duration) is not int or duration not in (18000, 604800):
            raise ValueError("Unknown subscription usage window.")
        reset = window["reset_at"]
        if type(reset) is not int:
            raise ValueError("Invalid subscription reset time.")
        windows.append(
            UsageWindow(
                "weekly" if duration == 604800 else "five_hour",
                percentage(window["used_percent"]),
                now,
                datetime.fromtimestamp(reset, UTC),
            )
        )
    return windows


def claude_windows(payload, now):
    windows = []
    for field, name in (("five_hour", "five_hour"), ("seven_day", "weekly")):
        window = payload.get(field)
        if window is None:
            continue
        reset = datetime.fromisoformat(window["resets_at"])
        if reset.tzinfo is None:
            raise ValueError("Subscription reset time requires a timezone.")
        windows.append(UsageWindow(name, percentage(window["utilization"]), now, reset))
    return windows


def read_subscription_usage(provider, files, now, *, opener=None):
    headers = {"Accept": "application/json", "User-Agent": "vibearr-subscription-usage"}
    if provider == "codex":
        tokens = files[".codex/auth.json"]["tokens"]
        headers.update(
            Authorization="Bearer " + tokens["access_token"],
            **{
                "ChatGPT-Account-Id": tokens["account_id"],
            },
        )
        url = "https://chatgpt.com/backend-api/wham/usage"
        parse = codex_windows
    elif provider == "claude":
        token = files[".claude/.credentials.json"]["claudeAiOauth"]["accessToken"]
        headers.update(
            Authorization="Bearer " + token, **{"anthropic-beta": "oauth-2025-04-20"}
        )
        url = "https://api.anthropic.com/api/oauth/usage"
        parse = claude_windows
    else:
        return UsageReading(supported=False)
    opener = opener or urllib.request.build_opener(
        urllib.request.ProxyHandler({}), NoRedirect()
    )
    request = urllib.request.Request(url, headers=headers)
    with opener.open(request, timeout=5) as response:
        content = response.read(32769)
        if len(content) > 32768:
            raise ValueError("Subscription usage response exceeds its bound.")
        payload = json.loads(content)
    return UsageReading(True, tuple(parse(payload, now)))


class SubscriptionUsage:
    def __init__(self, mediation):
        self.mediation = mediation

    def read_usage(self, account, now):
        result = self.mediation.request(
            "/v4/usage/read",
            {
                "provider": account["backend"],
                "credential_id": account["credential_id"],
            },
        )
        if result["supported"] is not True:
            return UsageReading(False)
        windows = tuple(
            UsageWindow(
                row["window_id"],
                row["remaining_percent"],
                datetime.fromisoformat(row["observed_at"]),
                datetime.fromisoformat(row["reset_at"]) if row["reset_at"] else None,
            )
            for row in result["windows"]
        )
        return UsageReading(True, windows)
