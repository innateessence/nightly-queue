import json
import importlib.util
import unittest

from vibearr_runner.native_sessions import NativeSessionError

HAS_EXECUTOR = importlib.util.find_spec("vibearr_executor") is not None
if HAS_EXECUTOR:
    from vibearr_runner.native_cli import NativeCLIAdapter, NativeEvents


@unittest.skipUnless(
    HAS_EXECUTOR, "Install the native-runtime extra for CLI execution tests"
)
class NativeEventTests(unittest.TestCase):
    def test_claude_success_uses_actual_session_identity(self):
        parser = NativeEvents("claude")
        events = [
            {"type": "system", "subtype": "init", "session_id": "real-session"},
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "session_id": "real-session",
                "usage": {"input_tokens": 10},
            },
        ]
        parser.feed(
            "\n".join(json.dumps(event) for event in events).encode(), final=True
        )
        self.assertTrue(parser.completed)
        self.assertEqual(parser.session_id, "real-session")
        self.assertEqual(parser.usage, {"input_tokens": 10})

    def test_claude_error_is_not_completion(self):
        parser = NativeEvents("claude")
        parser.feed(b'{"type":"result","subtype":"error_max_turns","is_error":true}\n')
        self.assertTrue(parser.failed)
        self.assertFalse(parser.completed)

    def test_opencode_tool_step_is_not_completion(self):
        parser = NativeEvents("opencode")
        parser.feed(
            b'{"type":"step_finish","sessionID":"ses_actual","part":{"reason":"tool-calls"}}\n'
        )
        self.assertFalse(parser.completed)

    def test_opencode_stop_retains_native_session_and_token_counts(self):
        parser = NativeEvents("opencode")
        parser.feed(
            b'{"type":"step_finish","sessionID":"ses_actual","part":{"reason":"stop","tokens":{"input":10,"output":2}}}\n'
        )
        self.assertTrue(parser.completed)
        self.assertEqual(parser.session_id, "ses_actual")
        self.assertEqual(parser.usage, {"input": 10, "output": 2})

    def test_native_session_identity_cannot_change(self):
        parser = NativeEvents("opencode")
        with self.assertRaises(NativeSessionError):
            parser.feed(
                b'{"type":"text","sessionID":"ses_first"}\n{"type":"text","sessionID":"ses_other"}\n'
            )


@unittest.skipUnless(
    HAS_EXECUTOR, "Install the native-runtime extra for CLI execution tests"
)
class NativeConnectionTests(unittest.TestCase):
    def test_codex_does_not_accept_api_key_connection(self):
        adapter = NativeCLIAdapter(None, "codex")
        with self.assertRaises(NativeSessionError):
            adapter.connection({"credential": {"provider": "codex", "kind": "token"}})

    def test_claude_does_not_accept_api_key_connection(self):
        adapter = NativeCLIAdapter(None, "claude")
        with self.assertRaises(NativeSessionError):
            adapter.connection({"credential": {"provider": "claude", "kind": "token"}})

    def test_opencode_does_not_accept_cloud_fallback(self):
        adapter = NativeCLIAdapter(None, "opencode")
        connection = {
            "credential": {"provider": "opencode", "kind": "connection", "id": 1},
            "material": {"secret": '{"provider":"openai","api_key":"secret"}'},
        }
        with self.assertRaises(NativeSessionError):
            adapter.connection(connection)
