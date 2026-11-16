import importlib.util
import json
import unittest
from uuid import uuid4

from .native_conformance import ExecutorFixture, NativeAdapterConformance
from vibearr_runner.native_claude import (
    BUILTIN_PLUGINS,
    CLAUDE_VERSION,
    ClaudeEvents,
    NativeClaudeAdapter,
    claude_profile,
)
from vibearr_runner.native_codex import NativeCodexAdapter, codex_profile
from vibearr_runner.native_sessions import NativeBackendRegistry, NativeSessionError

EXECUTOR_INSTALLED = importlib.util.find_spec("vibearr_executor") is not None


def init_event(model="claude-sonnet-5-5", session_id=None):
    return {
        "type": "system",
        "subtype": "init",
        "session_id": session_id or str(uuid4()),
        "claude_code_version": CLAUDE_VERSION,
        "model": model,
        "permissionMode": "dontAsk",
        "cwd": "/work",
        "tools": ["Bash", "Read", "Edit", "Write", "Glob", "Grep", "EndConversation"],
        "mcp_servers": [],
        "plugins": [],
        "skills": [],
        "slash_commands": [],
    }


def result_event(session_id, **overrides):
    return {
        "type": "result",
        "subtype": "success",
        "session_id": session_id,
        "is_error": False,
        "permission_denials": [],
        "usage": {"input_tokens": 12, "output_tokens": 4},
        **overrides,
    }


def encoded(*events):
    return b"".join(json.dumps(event).encode() + b"\n" for event in events)


@unittest.skipUnless(EXECUTOR_INSTALLED, "Install the native-runtime extra.")
class ClaudeConformanceTests(NativeAdapterConformance, unittest.TestCase):
    def configure_adapter(self):
        self.client = ExecutorFixture(claude_profile("sha256:" + "a" * 64, "token"))
        self.adapter = NativeClaudeAdapter(self.client)
        self.model = "claude-sonnet-5-5"
        self.secret = "fixture-claude-api-key-0123456789"
        self.credential = {
            "credential": {
                "id": 7,
                "provider": "claude",
                "kind": "token",
                "version": 2,
            },
            "material": {"secret": self.secret},
        }

    def completion(self):
        event = init_event(self.model)
        return encoded(event, result_event(event["session_id"]))

    def test_registry_selects_native_claude_without_proposal_transport(self):
        self.assertIsInstance(
            NativeBackendRegistry().create("claude", self.client), NativeClaudeAdapter
        )

    def test_progress_ignores_partial_final_log_line_until_completed(self):
        session = self.prepare()
        event = init_event(self.model)
        self.client.content = encoded(event) + b'{"type":"assistant","session_id":'
        self.assertEqual(
            self.adapter.events(session),
            [{"kind": "session.started", "session_id": event["session_id"]}],
        )

    def test_initialization_cannot_load_project_mcp(self):
        session = self.prepare()
        event = init_event(self.model)
        event["mcp_servers"] = [{"name": "untrusted", "status": "connected"}]
        self.client.content = encoded(event, result_event(event["session_id"]))
        self.client.complete(session.manifest["operation_id"])
        with self.assertRaises(NativeSessionError):
            self.adapter.outcome(session, self.source)

    def test_pinned_image_bundled_plugins_are_accepted_without_external_discovery(self):
        session = self.prepare()
        event = init_event(self.model)
        event["plugins"] = [
            {"name": name, "path": "builtin", "source": name + "@builtin"}
            for name in BUILTIN_PLUGINS
        ]
        self.client.content = encoded(event, result_event(event["session_id"]))
        self.client.complete(session.manifest["operation_id"])
        self.assertTrue(self.adapter.outcome(session, self.source)["successful"])

    def test_bundled_name_cannot_hide_an_inherited_plugin_path(self):
        session = self.prepare()
        event = init_event(self.model)
        event["plugins"] = [
            {
                "name": "cc-plugin-agents-md",
                "path": "/work/.claude/plugins/foreign",
                "source": "cc-plugin-agents-md@builtin",
            }
        ]
        self.client.content = encoded(event)
        with self.assertRaises(NativeSessionError):
            self.adapter.events(session)

    def test_initialization_cannot_change_selected_model(self):
        session = self.prepare()
        event = init_event("claude-foreign")
        self.client.content = encoded(event, result_event(event["session_id"]))
        with self.assertRaises(NativeSessionError):
            self.adapter.events(session)

    def test_oauth_is_unsupported_even_if_selected_as_token(self):
        self.request["credential"]["material"][
            "secret"
        ] = "sk-ant-oat01-01234567890123456789"
        with self.assertRaises(NativeSessionError):
            self.prepare()
        self.assertFalse(self.client.uploads)


@unittest.skipUnless(EXECUTOR_INSTALLED, "Install the native-runtime extra.")
class CodexCommonConformanceTests(NativeAdapterConformance, unittest.TestCase):
    def configure_adapter(self):
        self.client = ExecutorFixture(codex_profile("sha256:" + "a" * 64, "token"))
        self.adapter = NativeCodexAdapter(self.client)
        self.model = "gpt-6.1-sol"
        self.secret = "fixture-codex-api-key-0123456789"
        self.credential = {
            "credential": {"id": 7, "provider": "codex", "kind": "token", "version": 2},
            "material": {"secret": self.secret},
        }

    def completion(self):
        return encoded(
            {"type": "thread.started", "thread_id": str(uuid4())},
            {
                "type": "turn.completed",
                "usage": {"input_tokens": 12, "output_tokens": 4},
            },
        )


class ClaudeEventTests(unittest.TestCase):
    def test_incremental_utf8_and_json_are_reassembled(self):
        event = init_event()
        content = encoded(
            event,
            {
                "type": "assistant",
                "session_id": event["session_id"],
                "message": {"content": [{"type": "text", "text": "hello"}]},
            },
        )
        parser = ClaudeEvents()
        for byte in content:
            parser.feed(bytes([byte]))
        self.assertEqual(parser.events[-1]["text"], "hello")

    def test_duplicate_terminal_result_is_rejected(self):
        event = init_event()
        with self.assertRaises(NativeSessionError):
            ClaudeEvents().feed(
                encoded(
                    event,
                    result_event(event["session_id"]),
                    result_event(event["session_id"]),
                )
            )

    def test_conflicting_session_is_rejected(self):
        with self.assertRaises(NativeSessionError):
            ClaudeEvents().feed(encoded(init_event(), result_event(str(uuid4()))))

    def test_error_terminal_subtype_prevents_success(self):
        event = init_event()
        parser = ClaudeEvents()
        parser.feed(
            encoded(event, result_event(event["session_id"], subtype="error_max_turns"))
        )
        self.assertTrue(parser.failed)

    def test_error_flag_prevents_success(self):
        parser = ClaudeEvents()
        parser.feed(encoded(result_event(str(uuid4()), is_error=True)))
        self.assertTrue(parser.failed)

    def test_denied_tool_prevents_success(self):
        parser = ClaudeEvents()
        parser.feed(
            encoded(
                result_event(str(uuid4()), permission_denials=[{"tool_name": "Bash"}])
            )
        )
        self.assertTrue(parser.failed)

    def test_tool_activity_cannot_add_unconfigured_tool(self):
        event = {
            "type": "assistant",
            "session_id": str(uuid4()),
            "message": {"content": [{"type": "tool_use", "name": "mcp__foreign__run"}]},
        }
        with self.assertRaises(NativeSessionError):
            ClaudeEvents().feed(encoded(event))

    def test_incomplete_final_event_is_rejected(self):
        with self.assertRaises(NativeSessionError):
            ClaudeEvents().feed(b'{"type":', final=True)

    def test_output_and_line_bounds_are_enforced(self):
        for size in (256 * 1024 + 1, 1024 * 1024 + 1):
            with self.subTest(size=size), self.assertRaises(NativeSessionError):
                ClaudeEvents().feed(b"a" * size)

    def test_usage_is_terminal_total_without_double_counting_assistant_blocks(self):
        event = init_event()
        assistant = {
            "type": "assistant",
            "session_id": event["session_id"],
            "message": {"content": [], "usage": {"input_tokens": 100}},
        }
        parser = ClaudeEvents()
        parser.feed(
            encoded(
                event,
                assistant,
                assistant,
                result_event(
                    event["session_id"],
                    usage={
                        "input_tokens": 12,
                        "output_tokens": 4,
                        "cache_read_input_tokens": 7,
                        "cache_creation_input_tokens": 3,
                    },
                ),
            )
        )
        self.assertEqual(
            parser.usage,
            {
                "input_tokens": 12,
                "output_tokens": 4,
                "cache_read_input_tokens": 7,
                "cache_creation_input_tokens": 3,
            },
        )

    def test_boolean_negative_and_infinite_usage_are_rejected(self):
        for count in (True, -1, float("inf")):
            with self.subTest(count=count), self.assertRaises(NativeSessionError):
                ClaudeEvents().feed(
                    encoded(result_event(str(uuid4()), usage={"input_tokens": count}))
                )

    def test_wrapper_error_does_not_expose_provider_text(self):
        parser = ClaudeEvents()
        parser.feed(encoded({"type": "error", "message": "untrusted secret text"}))
        self.assertTrue(parser.failed)
        self.assertNotIn("untrusted secret text", json.dumps(parser.events))

    def test_actual_missing_auth_success_subtype_is_still_failed(self):
        parser = ClaudeEvents()
        parser.feed(
            encoded(
                result_event(
                    str(uuid4()),
                    is_error=True,
                    result="Not logged in · Please run /login",
                    usage={"input_tokens": 0, "output_tokens": 0},
                )
            )
        )
        self.assertTrue(parser.completed)
        self.assertTrue(parser.failed)
