import copy
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import json
import unittest
from uuid import uuid4

from vibearr_runner.native_opencode import (
    NativeOpenCodeAdapter,
    OpenCodeEvents,
    opencode_profile,
)
from vibearr_runner.native_sessions import (
    NativeBackendRegistry,
    NativeSession,
    NativeSessionError,
)
from vibearr_runner.policy import default_execution_policy
from .native_conformance import ExecutorFixture, NativeAdapterConformance

EXECUTOR_INSTALLED = importlib.util.find_spec("vibearr_executor") is not None
if EXECUTOR_INSTALLED:
    from vibearr_executor.artifacts import pack


def credential():
    return {
        "credential": {
            "id": 3,
            "provider": "opencode",
            "kind": "connection",
            "version": 2,
        },
        "material": {
            "secret": json.dumps(
                {
                    "provider": "openai",
                    "api_key": "fixture-opencode-key-0123456789",
                    "base_url": "https://api.openai.com/v1",
                }
            )
        },
    }


def event(kind, index, **fields):
    part_kind = "tool" if kind == "tool_use" else kind.replace("_", "-")
    return {
        "type": kind,
        "timestamp": 12345,
        "sessionID": "ses_fixture123",
        "part": {
            "id": "prt_" + str(index),
            "messageID": "msg_fixture",
            "sessionID": "ses_fixture123",
            "type": part_kind,
            **fields,
        },
    }


def finish(index=2, reason="stop"):
    return event(
        "step_finish",
        index,
        reason=reason,
        cost=0,
        tokens={
            "input": 10,
            "output": 3,
            "reasoning": 2,
            "cache": {"read": 4, "write": 0},
        },
    )


def content(*events):
    return ("\n".join(json.dumps(value) for value in events) + "\n").encode()


class OpenCodeEventTests(unittest.TestCase):
    def test_native_stop_and_token_usage(self):
        parser = OpenCodeEvents()
        parser.feed(content(event("step_start", 1), finish()), final=True)
        self.assertTrue(parser.completed)
        self.assertFalse(parser.failed)
        self.assertEqual(parser.session_id, "ses_fixture123")
        self.assertEqual(
            parser.usage,
            {
                "input": 10,
                "output": 3,
                "reasoning": 2,
                "cached_input": 4,
                "cache_write": 0,
            },
        )

    def test_tool_steps_do_not_complete_task(self):
        parser = OpenCodeEvents()
        parser.feed(
            content(event("step_start", 1), finish(reason="tool-calls")), final=True
        )
        self.assertFalse(parser.completed)

    def test_multiple_model_steps_accumulate_usage_once(self):
        parser = OpenCodeEvents()
        parser.feed(
            content(
                event("step_start", 1),
                finish(reason="tool-calls"),
                event("step_start", 3),
                finish(index=4),
            ),
            final=True,
        )
        self.assertEqual(parser.usage["input"], 20)
        self.assertTrue(parser.completed)

    def test_changed_session_is_rejected(self):
        changed = finish()
        changed["sessionID"] = "ses_foreign"
        parser = OpenCodeEvents()
        with self.assertRaises(NativeSessionError):
            parser.feed(content(event("step_start", 1), changed))

    def test_foreign_part_session_is_rejected(self):
        changed = finish()
        changed["part"]["sessionID"] = "ses_foreign"
        with self.assertRaises(NativeSessionError):
            OpenCodeEvents().feed(content(event("step_start", 1), changed))

    def test_duplicate_token_part_is_rejected(self):
        with self.assertRaises(NativeSessionError):
            OpenCodeEvents().feed(content(event("step_start", 1), finish(), finish()))

    def test_truncated_step_is_not_success(self):
        parser = OpenCodeEvents()
        parser.feed(content(event("step_start", 1)), final=True)
        self.assertFalse(parser.completed)

    def test_partial_progress_waits_for_complete_line(self):
        encoded = content(event("step_start", 1), finish())
        parser = OpenCodeEvents()
        parser.feed(encoded[:-8])
        self.assertFalse(parser.completed)
        parser.feed(encoded[-8:])
        self.assertTrue(parser.completed)

    def test_error_prevents_success(self):
        parser = OpenCodeEvents()
        parser.feed(
            content(
                event("step_start", 1),
                finish(),
                {"type": "error", "error": {"name": "ProviderAuthError"}},
            )
        )
        self.assertTrue(parser.failed)

    def test_length_limit_is_unsuccessful(self):
        parser = OpenCodeEvents()
        parser.feed(content(event("step_start", 1), finish(reason="length")))
        self.assertTrue(parser.failed)
        self.assertFalse(parser.completed)

    def test_non_integer_or_negative_usage_is_rejected(self):
        for amount in (-1, True, 1.1, float("nan"), 2**63):
            with self.subTest(amount=amount):
                value = finish()
                value["part"]["tokens"]["input"] = amount
                with self.assertRaises(NativeSessionError):
                    OpenCodeEvents().feed(content(event("step_start", 1), value))

    def test_completion_without_start_is_rejected(self):
        with self.assertRaises(NativeSessionError):
            OpenCodeEvents().feed(content(finish()))

    def test_output_and_line_limits_are_enforced(self):
        for size in (256 * 1024 + 1, 1024 * 1024 + 1):
            with self.subTest(size=size):
                with self.assertRaises(NativeSessionError):
                    OpenCodeEvents().feed(b"x" * size)

    def test_incomplete_final_event_cannot_follow_success(self):
        parser = OpenCodeEvents()
        with self.assertRaises(NativeSessionError):
            parser.feed(
                content(event("step_start", 1), finish()) + b'{"type":"error"',
                final=True,
            )

    def test_foreign_session_error_is_rejected(self):
        with self.assertRaises(NativeSessionError):
            OpenCodeEvents().feed(
                content(
                    event("step_start", 1),
                    {"type": "error", "sessionID": "ses_foreign"},
                )
            )


@unittest.skipUnless(EXECUTOR_INSTALLED, "Install the native-runtime extra.")
class NativeOpenCodeTests(unittest.TestCase):
    def setUp(self):
        self.client = ExecutorFixture(opencode_profile("sha256:" + "a" * 64))
        self.adapter = NativeOpenCodeAdapter(self.client)
        self.policy = default_execution_policy()
        self.policy["runtime_profile"] = self.client.profile.profile_id
        self.source = pack({"answer.py": (0o644, b"answer = 0\n")})
        self.request = {
            "run_id": str(uuid4()),
            "operation_id": str(uuid4()),
            "source": self.source,
            "prompt": "Set answer to 42.",
            "model": "openai/gpt-6.1-sol",
            "policy": self.policy,
            "deadline": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
            "credential": credential(),
        }

    def complete(self):
        session = self.adapter.prepare(**self.request)
        self.client.content = content(event("step_start", 1), finish())
        self.client.complete(session.manifest["operation_id"])
        return session

    def test_registry_uses_native_opencode(self):
        self.assertIsInstance(
            NativeBackendRegistry().create("opencode", self.client),
            NativeOpenCodeAdapter,
        )

    def test_selected_provider_model_and_connection_are_preserved(self):
        session = self.adapter.prepare(**self.request)
        self.assertEqual(
            session.account,
            {
                "backend": "opencode",
                "credential_id": 3,
                "credential_version": 2,
                "model": "openai/gpt-6.1-sol",
            },
        )
        self.assertNotIn("fixture-opencode-key", json.dumps(session.to_data()))
        self.assertEqual(session.auth_kind, "connection")

    def test_unsupported_connection_cannot_prepare(self):
        self.request["credential"]["material"]["secret"] = json.dumps(
            {"provider": "anthropic", "api_key": "fixture-anthropic-key-0123456789"}
        )
        with self.assertRaises(NativeSessionError):
            self.adapter.prepare(**self.request)
        self.assertEqual(self.client.prepared, {})

    def test_wrong_provider_model_cannot_prepare(self):
        self.request["model"] = "anthropic/claude-sonnet"
        with self.assertRaises(ValueError):
            self.adapter.prepare(**self.request)
        self.assertEqual(self.client.prepared, {})

    def test_ordinary_connection_does_not_claim_authenticated(self):
        result = self.adapter.diagnostics(
            self.policy, credential(), model=self.request["model"]
        )
        self.assertTrue(result["auth_available"])
        self.assertFalse(result["authenticated"])
        self.assertFalse(result["native_agent_ready"])

    def test_foreign_provider_model_is_not_locally_available(self):
        result = self.adapter.diagnostics(
            self.policy, credential(), model="anthropic/claude"
        )
        self.assertFalse(result["auth_available"])
        self.assertFalse(result["authenticated"])

    def test_restart_reconciles_same_operation_without_starting_model(self):
        original = self.adapter.prepare(**self.request)
        self.adapter.start(original)
        restored = NativeSession.from_data(original.to_data())
        adapter = NativeOpenCodeAdapter(self.client)
        self.assertEqual(adapter.reconcile(restored)["state"], "running")
        adapter.start(restored)
        self.assertEqual(self.client.starts, 1)
        self.assertEqual(len(self.client.prepared), 1)

    def test_success_requires_protected_termination_and_log_digest(self):
        session = self.complete()
        self.assertTrue(self.adapter.outcome(session, self.source)["successful"])
        self.client.receipt["log_digest"] = "0" * 64
        with self.assertRaises(NativeSessionError):
            self.adapter.outcome(session, self.source)

    def test_intermediate_tool_finish_cannot_succeed(self):
        session = self.complete()
        self.client.content = content(
            event("step_start", 1), finish(reason="tool-calls")
        )
        self.client.complete(session.manifest["operation_id"])
        self.assertFalse(self.adapter.outcome(session, self.source)["successful"])

    def test_cancellation_reports_descendants_terminated(self):
        session = self.adapter.prepare(**self.request)
        self.adapter.start(session)
        observed = self.adapter.cancel(session)
        self.assertEqual(observed["state"], "exited")
        self.assertTrue(observed["descendants_terminated"])
        self.assertTrue(observed["result"]["canceled"])

    def test_other_adapter_cannot_reconnect(self):
        session = self.adapter.prepare(**self.request)
        foreign = copy.deepcopy(session.to_data())
        foreign["adapter"] = "codex"
        with self.assertRaises(NativeSessionError):
            self.adapter.reconcile(NativeSession.from_data(foreign))

    def test_active_outcome_remains_fenced(self):
        session = self.adapter.prepare(**self.request)
        self.adapter.start(session)
        with self.assertRaises(NativeSessionError):
            self.adapter.outcome(session, self.source)

    def test_progress_does_not_finalize_partial_record(self):
        session = self.adapter.prepare(**self.request)
        self.client.content = content(event("step_start", 1), finish())[:-8]
        self.assertEqual(self.adapter.events(session)[-1]["kind"], "turn.started")

    def test_unqualified_profile_prevents_prepare(self):
        self.client.qualified = False
        with self.assertRaises(NativeSessionError):
            self.adapter.prepare(**self.request)

    def test_no_remaining_percentage_is_reported(self):
        session = self.complete()
        result = self.adapter.outcome(session, self.source)
        self.assertFalse(result["quota_supported"])
        self.assertEqual(result["usage_unit"], "tokens")
        self.assertEqual(
            hashlib.sha256(self.client.content).hexdigest(), result["log_digest"]
        )


@unittest.skipUnless(EXECUTOR_INSTALLED, "Install the native-runtime extra.")
class OpenCodeConformanceTests(NativeAdapterConformance, unittest.TestCase):
    def configure_adapter(self):
        self.client = ExecutorFixture(opencode_profile("sha256:" + "a" * 64))
        self.adapter = NativeOpenCodeAdapter(self.client)
        self.model = "openai/gpt-6.1-sol"
        self.credential = credential()
        self.credential["credential"]["id"] = 7
        self.secret = "fixture-opencode-key-0123456789"

    def completion(self):
        return content(event("step_start", 1), finish())

    def expected_usage(self):
        return {
            "input": 10,
            "output": 3,
            "reasoning": 2,
            "cached_input": 4,
            "cache_write": 0,
        }
