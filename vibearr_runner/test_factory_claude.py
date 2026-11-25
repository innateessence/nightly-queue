"""Claude's pinned stream and argv contracts, without provider requests."""

import importlib.util
import hashlib
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from vibearr_runner.test_native_conformance import RecordedExecutor

from vibearr_runner.native_conformance import (
    FACTORY_ROLES,
    FactoryNativeAdapter,
    parse_role_result,
)
from vibearr_runner.native_codex import MAX_EVENTS, MAX_LINE, MAX_OUTPUT
from vibearr_runner.native_sessions import NativeSessionError
from vibearr_runner.policy import default_execution_policy


SDK_AVAILABLE = importlib.util.find_spec("vibearr_executor") is not None
if SDK_AVAILABLE:
    from vibearr_executor.claude_task import ALTERNATE_AUTH, READ_ONLY_ROLES
    from vibearr_executor.native_task import command
    from vibearr_runner.native_cli import NativeCLIAdapter, NativeEvents


SESSION = "dcb7bcf8-6abc-49ba-9cc7-0c223bff2b75"


def completion(**changes):
    return {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "session_id": SESSION,
        "result": '{"verdict":"pass"}',
        "usage": {"input_tokens": 12, "output_tokens": 4},
        **changes,
    }


def transcript(*events):
    return b"\n".join(json.dumps(event).encode() for event in events) + b"\n"


@unittest.skipUnless(
    SDK_AVAILABLE, "Install the native-runtime extra for native executor tests"
)
class ClaudeFactoryIntegrationTests(unittest.TestCase):
    def test_every_claude_role_uses_the_common_adapter_and_protected_outcome(self):
        for role in FACTORY_ROLES:
            with self.subTest(role=role):
                executor = RecordedExecutor()
                delegate = NativeCLIAdapter(executor, "claude")
                adapter = FactoryNativeAdapter(delegate, role, executor.capabilities())
                session = adapter.prepare(
                    run_id=str(uuid4()),
                    operation_id=str(uuid4()),
                    source=executor.source,
                    prompt="Return the required role JSON.",
                    model="claude-sonnet-4-6",
                    policy=default_execution_policy(),
                    deadline=(
                        datetime.now(timezone.utc) + timedelta(minutes=5)
                    ).isoformat(),
                    credential={
                        "credential": {
                            "provider": "claude",
                            "kind": "login",
                            "id": 7,
                            "version": 2,
                        },
                        "material": {},
                    },
                )
                context = json.loads(executor.uploads["context"])
                self.assertEqual(context["factory_role"], role)
                self.assertEqual(context["credential_id"], 7)
                executor.content = transcript(completion())
                executor.complete(session.manifest["operation_id"])
                outcome = adapter.outcome(session, executor.source)
                self.assertTrue(outcome["successful"])
                self.assertEqual(outcome["role"], role)
                self.assertEqual(outcome["role_result"], {"verdict": "pass"})


@unittest.skipUnless(
    SDK_AVAILABLE, "Install the native-runtime extra for native executor tests"
)
class ClaudeFactoryEventTests(unittest.TestCase):
    def test_common_factory_parser_consumes_the_complete_claude_result(self):
        payload = {"verdict": "pass", "findings": ["evidence " * 1000]}
        logs = transcript(completion(result=json.dumps(payload)))
        events = NativeEvents("claude")
        events.feed(logs, final=True)
        role_result = parse_role_result("claude", logs)
        self.assertEqual(role_result["payload"], payload)
        self.assertEqual(role_result["session_id"], events.session_id)
        self.assertEqual(role_result["log_digest"], hashlib.sha256(logs).hexdigest())

    def test_terminal_result_keeps_complete_payload_separate_from_display_preview(self):
        result = json.dumps({"findings": ["evidence " * 1000]})
        parser = NativeEvents("claude")
        parser.feed(
            transcript(
                {
                    "type": "assistant",
                    "session_id": SESSION,
                    "message": {"content": [{"type": "text", "text": "x" * 5000}]},
                },
                completion(result=result),
            ),
            final=True,
        )
        self.assertEqual(parser.complete_result, result)
        self.assertEqual(len(parser.events[1]["text"]), 4000)
        self.assertTrue(parser.completed)

    def test_reported_usage_keeps_actual_token_counters_only(self):
        parser = NativeEvents("claude")
        parser.feed(
            transcript(
                completion(
                    usage={
                        "input_tokens": 2,
                        "output_tokens": 3,
                        "cache_creation_input_tokens": 4,
                        "cache_read_input_tokens": 5,
                        "service_tier": "standard",
                        "server_tool_use": {"web_search_requests": 1},
                    }
                )
            ),
            final=True,
        )
        self.assertEqual(
            parser.usage,
            {
                "input_tokens": 2,
                "output_tokens": 3,
                "cache_creation_input_tokens": 4,
                "cache_read_input_tokens": 5,
            },
        )

    def test_missing_usage_remains_unavailable_instead_of_inventing_tokens(self):
        event = completion()
        del event["usage"]
        parser = NativeEvents("claude")
        parser.feed(transcript(event), final=True)
        self.assertEqual(parser.usage, {})

    def test_duplicate_success_is_rejected(self):
        with self.assertRaises(NativeSessionError):
            NativeEvents("claude").feed(
                transcript(completion(), completion()), final=True
            )

    def test_error_after_success_is_rejected(self):
        with self.assertRaises(NativeSessionError):
            NativeEvents("claude").feed(
                transcript(completion(), {"type": "error", "error": "failed"}),
                final=True,
            )

    def test_error_before_success_cannot_be_overwritten(self):
        parser = NativeEvents("claude")
        parser.feed(
            transcript({"type": "error", "error": "failed"}, completion()), final=True
        )
        self.assertFalse(parser.completed)
        self.assertTrue(parser.failed)

    def test_inconsistent_terminal_flags_are_rejected(self):
        for changes in ({"is_error": True}, {"subtype": "error_max_turns"}):
            with self.subTest(changes=changes), self.assertRaises(NativeSessionError):
                NativeEvents("claude").feed(
                    transcript(completion(**changes)), final=True
                )

    def test_success_requires_session_identity(self):
        event = completion()
        del event["session_id"]
        with self.assertRaises(NativeSessionError):
            NativeEvents("claude").feed(transcript(event), final=True)

    def test_session_identity_cannot_change(self):
        with self.assertRaises(NativeSessionError):
            NativeEvents("claude").feed(
                transcript(
                    {"type": "system", "subtype": "init", "session_id": SESSION},
                    completion(session_id="another-session"),
                ),
                final=True,
            )

    def test_terminal_result_requires_its_own_session_identity(self):
        event = completion()
        del event["session_id"]
        with self.assertRaises(NativeSessionError):
            NativeEvents("claude").feed(
                transcript(
                    {"type": "system", "subtype": "init", "session_id": SESSION},
                    event,
                ),
                final=True,
            )

    def test_success_requires_complete_result_text(self):
        for result in (None, 1, [], {}, "", "  "):
            with self.subTest(result=result), self.assertRaises(NativeSessionError):
                NativeEvents("claude").feed(
                    transcript(completion(result=result)), final=True
                )

    def test_malformed_session_identity_is_rejected(self):
        for identity in (None, True, 3, "", "bad\nsession", "a" * 201):
            with self.subTest(identity=identity), self.assertRaises(NativeSessionError):
                NativeEvents("claude").feed(
                    transcript(completion(session_id=identity)), final=True
                )

    def test_malformed_token_usage_is_rejected(self):
        for count in (True, -1, 1.0, "1", 2**63):
            with self.subTest(count=count), self.assertRaises(NativeSessionError):
                NativeEvents("claude").feed(
                    transcript(completion(usage={"input_tokens": count})), final=True
                )

    def test_non_object_usage_is_rejected(self):
        with self.assertRaises(NativeSessionError):
            NativeEvents("claude").feed(transcript(completion(usage=[])), final=True)

    def test_duplicate_json_field_is_rejected(self):
        with self.assertRaises(NativeSessionError):
            NativeEvents("claude").feed(
                b'{"type":"error","type":"result"}\n', final=True
            )

    def test_nonfinite_json_is_rejected(self):
        for constant in (b"NaN", b"Infinity", b"1e9999"):
            with self.subTest(constant=constant), self.assertRaises(NativeSessionError):
                NativeEvents("claude").feed(
                    b'{"type":"system","value":' + constant + b"}\n", final=True
                )

    def test_truncated_final_json_is_rejected(self):
        with self.assertRaises(NativeSessionError):
            NativeEvents("claude").feed(b'{"type":"result"', final=True)

    def test_oversized_output_is_rejected(self):
        with self.assertRaises(NativeSessionError):
            NativeEvents("claude").feed(b"x" * (MAX_OUTPUT + 1), final=True)

    def test_whitespace_line_cannot_bypass_the_event_byte_limit(self):
        with self.assertRaises(NativeSessionError):
            NativeEvents("claude").line(b" " * (MAX_LINE + 1))

    def test_event_limit_includes_the_session_started_event(self):
        parser = NativeEvents("claude")
        parser.events = [{"kind": "system"}] * (MAX_EVENTS - 1)
        with self.assertRaises(NativeSessionError):
            parser.feed(transcript(completion()), final=True)
        self.assertEqual(len(parser.events), MAX_EVENTS)

    def test_assistant_text_must_be_a_string(self):
        with self.assertRaises(NativeSessionError):
            NativeEvents("claude").feed(
                transcript(
                    {
                        "type": "assistant",
                        "session_id": SESSION,
                        "message": {"content": [{"type": "text", "text": 1}]},
                    }
                ),
                final=True,
            )

    def test_non_boolean_error_flag_is_rejected(self):
        with self.assertRaises(NativeSessionError):
            NativeEvents("claude").feed(
                transcript(completion(is_error="false")), final=True
            )

    def test_success_looking_text_never_overrides_actual_nonzero_exit(self):
        source = b"original source"
        logs = transcript(completion())
        client = Mock()
        client.logs.side_effect = lambda _operation, offset, _limit: logs[offset:]
        client.export.return_value = (
            {"result_digest": hashlib.sha256(source).hexdigest()},
            source,
        )
        adapter = NativeCLIAdapter(client, "claude")
        session = SimpleNamespace(
            manifest={
                "operation_id": "original-operation",
                "source_digest": hashlib.sha256(source).hexdigest(),
            }
        )
        state = {
            "state": "exited",
            "descendants_terminated": True,
            "result": {
                "exit_code": 1,
                "timed_out": False,
                "canceled": False,
                "log_truncated": False,
                "log_digest": hashlib.sha256(logs).hexdigest(),
            },
        }
        with (
            patch.object(adapter, "bound", return_value="original-operation"),
            patch.object(adapter, "inspect", return_value=state),
        ):
            outcome = adapter.outcome(session, source)
        self.assertFalse(outcome["successful"])

    def test_success_looking_text_cannot_complete_uncertain_descendants(self):
        source = b"original source"
        adapter = NativeCLIAdapter(Mock(), "claude")
        session = SimpleNamespace(
            manifest={"source_digest": hashlib.sha256(source).hexdigest()}
        )
        with patch.object(
            adapter,
            "inspect",
            return_value={"state": "exited", "descendants_terminated": False},
        ):
            with self.assertRaises(NativeSessionError):
                adapter.outcome(session, source)


@unittest.skipUnless(
    SDK_AVAILABLE, "Install the native-runtime extra for native executor tests"
)
class ClaudeFactoryCommandTests(unittest.TestCase):
    def arguments(self, role=None, **environment):
        if role is not None:
            environment["VIBEARR_FACTORY_ROLE"] = role
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, environment, clear=True),
        ):
            return command(
                "claude", "claude-sonnet-4-6", Path(directory), factory_role=role
            )

    def test_only_build_role_has_builder_tool_permissions(self):
        arguments = self.arguments("build")
        self.assertIn("bypassPermissions", arguments)
        self.assertNotIn("--tools", arguments)

    def test_every_read_only_role_restricts_available_tools_without_prompts(self):
        for role in sorted(READ_ONLY_ROLES):
            with self.subTest(role=role):
                arguments = self.arguments(role)
                self.assertEqual(
                    arguments[arguments.index("--tools") + 1], "Read,Glob,Grep"
                )
                self.assertIn("dontAsk", arguments)
                self.assertNotIn("bypassPermissions", arguments)

    def test_unregistered_role_is_rejected(self):
        with self.assertRaises(ValueError):
            self.arguments("publish")

    def test_context_cannot_select_role_without_a_registered_worker_role(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, {}, clear=True),
        ):
            with self.assertRaises(ValueError):
                command("claude", "sonnet", Path(directory), factory_role="build")

    def test_context_cannot_upgrade_a_read_only_profile(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(
                os.environ, {"VIBEARR_FACTORY_ROLE": "reviewer-code"}, clear=True
            ),
        ):
            with self.assertRaises(ValueError):
                command("claude", "sonnet", Path(directory), factory_role="build")

    def test_alternate_authentication_and_endpoints_are_rejected(self):
        for name in ALTERNATE_AUTH:
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.arguments("build", **{name: "unexpected"})

    def test_configuration_is_explicit_and_disables_hook_plugin_browser_discovery(self):
        arguments = self.arguments("reviewer-ui")
        settings = json.loads(arguments[arguments.index("--settings") + 1])
        self.assertTrue(settings["disableAllHooks"])
        self.assertEqual(settings["enabledPlugins"], {})
        self.assertEqual(arguments[arguments.index("--setting-sources") + 1], "user")
        self.assertIn("--strict-mcp-config", arguments)
        self.assertIn("--disable-slash-commands", arguments)
        self.assertIn("--no-chrome", arguments)
        self.assertIn("--no-session-persistence", arguments)
        self.assertNotIn("--bare", arguments)
        self.assertNotIn("--resume", arguments)

    def test_managed_subscription_configuration_uses_only_fresh_home(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(
                os.environ, {"CLAUDE_CONFIG_DIR": "/untrusted/config"}, clear=True
            ),
        ):
            command("claude", "sonnet", Path(directory))
            self.assertEqual(os.environ["CLAUDE_CONFIG_DIR"], directory + "/.claude")
