from datetime import UTC, datetime, timedelta
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from .backend_registry import BackendRegistry
from .gates import UsageReading, UsageWindow
from .subscription_usage import NoRedirect, SubscriptionUsage, read_subscription_usage


NOW = datetime(2026, 11, 26, tzinfo=UTC)
RESET = NOW + timedelta(hours=1)


def codex_payload():
    return {
        "rate_limit": {
            "primary_window": {
                "used_percent": 90,
                "limit_window_seconds": 18000,
                "reset_at": int(RESET.timestamp()),
            },
            "secondary_window": {
                "used_percent": 60,
                "limit_window_seconds": 604800,
                "reset_at": int(RESET.timestamp()),
            },
        }
    }


def login_files():
    return {
        ".codex/auth.json": {
            "auth_mode": "chatgpt",
            "tokens": {
                "access_token": "selected-access-token",
                "refresh_token": "selected-refresh-token",
                "id_token": "selected-identity-token",
                "account_id": "selected-account",
            },
        }
    }


class SubscriptionUsageTests(unittest.TestCase):
    def read(self, provider, files, payload):
        opener = Mock()
        opener.open.return_value = io.BytesIO(json.dumps(payload).encode())
        reading = read_subscription_usage(provider, files, NOW, opener=opener)
        request = opener.open.call_args.args[0]
        self.assertEqual(opener.open.call_args.kwargs["timeout"], 5)
        self.assertEqual(request.get_method(), "GET")
        return reading, request

    def test_codex_reads_selected_subscription_and_converts_used_to_remaining(self):
        reading, request = self.read("codex", login_files(), codex_payload())
        self.assertEqual(request.full_url, "https://chatgpt.com/backend-api/wham/usage")
        self.assertEqual(
            request.get_header("Authorization"), "Bearer selected-access-token"
        )
        self.assertEqual(request.get_header("Chatgpt-account-id"), "selected-account")
        self.assertEqual(
            [(w.window_id, w.remaining_percent) for w in reading.windows],
            [("five_hour", 10), ("weekly", 40)],
        )
        self.assertTrue(
            all(w.observed_at == NOW and w.reset_at == RESET for w in reading.windows)
        )

    def test_codex_classifies_duration_instead_of_primary_position(self):
        payload = codex_payload()
        limits = payload["rate_limit"]
        limits["primary_window"], limits["secondary_window"] = (
            limits["secondary_window"],
            limits["primary_window"],
        )
        reading, _ = self.read("codex", login_files(), payload)
        self.assertEqual(reading.windows[0].window_id, "weekly")

    def test_claude_reads_oauth_usage_without_model_call(self):
        files = {
            ".claude/.credentials.json": {
                "claudeAiOauth": {"accessToken": "claude-selected"}
            }
        }
        payload = {
            "five_hour": {"utilization": 25, "resets_at": RESET.isoformat()},
            "seven_day": {"utilization": 60, "resets_at": RESET.isoformat()},
        }
        reading, request = self.read("claude", files, payload)
        self.assertEqual(request.full_url, "https://api.anthropic.com/api/oauth/usage")
        self.assertEqual(request.get_header("Authorization"), "Bearer claude-selected")
        self.assertEqual([w.remaining_percent for w in reading.windows], [75, 40])

    def test_invalid_or_unknown_telemetry_is_rejected(self):
        for field, value in (
            ("used_percent", True),
            ("used_percent", -1),
            ("used_percent", 101),
            ("used_percent", float("nan")),
            ("limit_window_seconds", 3600),
            ("reset_at", "tomorrow"),
        ):
            payload = codex_payload()
            payload["rate_limit"]["primary_window"][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                self.read("codex", login_files(), payload)

    def test_redirects_and_oversized_responses_are_rejected(self):
        with self.assertRaises(ValueError):
            NoRedirect().redirect_request(
                None, None, 302, "Found", {}, "https://other.example"
            )
        opener = Mock()
        opener.open.return_value = io.BytesIO(b" " * 32769)
        with self.assertRaises(ValueError):
            read_subscription_usage("codex", login_files(), NOW, opener=opener)

    def test_authenticated_usage_channel_returns_no_login_material(self):
        try:
            from vibearr_executor.contracts import Rejected
            from .compact_connections import (
                CompactConnections,
                ConnectionServer,
                MediationClient,
            )
        except ModuleNotFoundError:
            self.skipTest(
                "Install the native-runtime extra for private usage channel checks."
            )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manager = CompactConnections(
                root / "connections", object(), login_root=root / "logins"
            )
            path = root / "logins/codex/7"
            path.mkdir(parents=True)
            (path / "login.json").write_text(json.dumps(login_files()))
            server = ConnectionServer(("127.0.0.1", 0), manager, "c" * 48, "w" * 48)
            thread = threading.Thread(target=server.serve_forever)
            thread.start()
            try:
                origin = f"http://127.0.0.1:{server.server_port}"
                token_path = root / "controller-token"
                token_path.write_text("c" * 48)
                with patch.dict(
                    "os.environ",
                    {
                        "VIBEARR_MEDIATION_URL": origin,
                        "VIBEARR_MEDIATION_CONTROLLER_TOKEN_FILE": str(token_path),
                    },
                ):
                    adapter = BackendRegistry().usage_adapters(
                        [{"backend": "codex", "credential_id": 7}]
                    )[("codex", 7)]
                self.assertIsInstance(adapter, SubscriptionUsage)
                reading = UsageReading(
                    True,
                    (
                        UsageWindow("weekly", 40, NOW, RESET),
                        UsageWindow("five_hour", 10, NOW, RESET),
                    ),
                )
                with patch(
                    "vibearr_runner.subscription_usage.read_subscription_usage",
                    return_value=reading,
                ) as fetch:
                    actual = adapter.read_usage(
                        {"backend": "codex", "credential_id": 7}, NOW
                    )
                    self.assertEqual(actual, reading)
                    self.assertEqual(fetch.call_args.args[:2], ("codex", login_files()))
                    self.assertNotIn("selected-access-token", repr(actual))
                    with self.assertRaises(Rejected):
                        MediationClient(origin, "w" * 48).request(
                            "/v4/usage/read", {"provider": "codex", "credential_id": 7}
                        )
                    self.assertEqual(fetch.call_count, 1)
                    with self.assertRaises(Rejected):
                        adapter.read_usage(
                            {"backend": "codex", "credential_id": 8}, NOW
                        )
                    self.assertEqual(fetch.call_count, 1)
            finally:
                server.shutdown()
                thread.join()
                server.server_close()
                manager.close()
