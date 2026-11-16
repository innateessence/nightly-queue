import copy
import io
import json
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock, patch
from uuid import uuid4

from . import claude_worker, test_backends as codex_tests
from .approvals import ActionSpec, ApprovalRequired
from .backend_registry import BackendRegistry, ScopedCredentials
from .backends import (
    BackendAuthenticationError,
    BackendError,
    BackendInterrupted,
    BackendOutputError,
    BackendReadiness,
    BackendRequest,
    BackendTimeout,
)
from .claude_backend import (
    CAPABILITIES,
    ClaudeBackend,
    ClaudeProcessTransport,
    parse_response,
    probe_claude,
    valid_model,
)
from .codex_backend import CodexBackend
from .gates import GateEvaluator, UsageWindow
from .journal import Journal
from .proposal_backend import ProcessTransport
from .test_gates import CONFIG, NOW, FakeUsage

ACCOUNT = {"backend": "claude", "credential_id": 13}
TOKEN = "selected-claude-secret"


def grant(account):
    return {
        "credential": {
            "id": account["credential_id"],
            "provider": account["backend"],
            "kind": "token",
            "version": 1,
        },
        "material": {"secret": TOKEN, "passphrase": ""},
    }


def response(kind="read", data=None):
    proposal = {"kind": kind, "data": {"path": "app.py"} if data is None else data}
    return {
        "response": {
            "type": "message",
            "role": "assistant",
            "stop_reason": "end_turn",
            "content": [{"type": "text", "text": json.dumps(proposal)}],
        }
    }


class AdapterContract:
    provider = "claude"
    factory: Any = ClaudeBackend
    envelope = staticmethod(response)

    def setUp(self):
        self.account = {"backend": self.provider, "credential_id": 13}
        self.transport = Mock(spec=ProcessTransport)
        self.transport.exchange.return_value = self.envelope()
        self.backend = self.factory(grant, transport=self.transport)
        self.request = BackendRequest(
            self.account, "claude-sonnet-5-5", TOKEN, {"source": TOKEN}
        )
        self.progress = Mock()

    def run_stage(self, stage="research", **kwargs):
        options = {
            "progress": self.progress,
            "checkpoint": lambda: True,
            "stop": threading.Event(),
            **kwargs,
        }
        return getattr(self.backend, stage)(self.request, **options)

    def test_read_proposal(self):
        self.assertEqual(self.run_stage().kind, "read")

    def test_implementation_complete(self):
        self.transport.exchange.return_value = self.envelope(
            "complete", {"summary": "Done"}
        )
        self.assertEqual(self.run_stage("implementation").kind, "complete")

    def test_research_forbids_complete(self):
        self.transport.exchange.return_value = self.envelope(
            "complete", {"summary": "Done"}
        )
        with self.assertRaises(BackendOutputError):
            self.run_stage()

    def test_no_secret_in_prompt_or_progress(self):
        self.run_stage()
        self.assertNotIn(
            TOKEN, json.dumps(self.transport.exchange.call_args.args[0]["request"])
        )
        self.assertNotIn(TOKEN, str(self.progress.call_args_list))

    def test_explicit_account_is_required(self):
        self.request = replace(
            self.request, account={"backend": self.provider, "credential_id": None}
        )
        with self.assertRaises(BackendAuthenticationError):
            self.run_stage()
        self.transport.exchange.assert_not_called()

    def test_other_provider_secret_cannot_be_reused(self):
        selected = grant(self.account)
        selected["credential"]["provider"] = "unselected"
        self.backend.credentials = lambda account: selected
        with self.assertRaises(BackendAuthenticationError):
            self.run_stage()

    def test_rotated_or_unavailable_secret_does_not_fallback(self):
        self.backend.credentials = Mock(
            side_effect=BackendAuthenticationError("revoked")
        )
        with self.assertRaises(BackendAuthenticationError):
            self.run_stage()
        self.transport.exchange.assert_not_called()

    def test_auth_error_is_redacted(self):
        self.transport.exchange.return_value = {"error": "authentication"}
        with self.assertRaises(BackendAuthenticationError) as failure:
            self.run_stage()
        self.assertNotIn(TOKEN, str(failure.exception))

    def test_provider_error_is_redacted(self):
        self.transport.exchange.return_value = {"error": TOKEN}
        with self.assertRaises(BackendError) as failure:
            self.run_stage()
        self.assertNotIn(TOKEN, str(failure.exception))

    def test_pause_blocks_model(self):
        with self.assertRaises(BackendInterrupted):
            self.run_stage(checkpoint=lambda: False)
        self.transport.exchange.assert_not_called()

    def test_cancel_blocks_model(self):
        stop = threading.Event()
        stop.set()
        with self.assertRaises(BackendInterrupted):
            self.run_stage(stop=stop)
        self.transport.exchange.assert_not_called()

    def test_interrupt_reaches_process(self):
        self.backend.interrupt()
        self.transport.interrupt.assert_called_once_with()

    def test_token_counts_do_not_claim_allowance(self):
        self.assertFalse(self.backend.read_usage(self.account, NOW).supported)
        self.transport.exchange.assert_not_called()

    def test_structural_secret_does_not_claim_live_readiness(self):
        readiness = self.backend.readiness(self.account)
        self.assertFalse(readiness.ready)
        self.assertFalse(readiness.authenticated)


class ClaudeContractTests(AdapterContract, unittest.TestCase):
    pass


class CodexContractTests(AdapterContract, unittest.TestCase):
    provider = "codex"
    factory = CodexBackend
    envelope = staticmethod(codex_tests.response)


class ClaudeSchemaTests(unittest.TestCase):
    def test_native_tool_content_is_rejected(self):
        for kind in (
            "tool_use",
            "server_tool_use",
            "bash_code_execution_tool_result",
            "web_search_tool_result",
        ):
            with self.subTest(kind=kind), self.assertRaises(BackendOutputError):
                value = response()
                value["response"]["content"].insert(0, {"type": kind})
                parse_response(value, "research")

    def test_incomplete_and_refused_responses_are_rejected(self):
        for reason in ("max_tokens", "tool_use", "refusal", "pause_turn", None):
            with self.subTest(reason=reason), self.assertRaises(BackendOutputError):
                value = response()
                value["response"]["stop_reason"] = reason
                parse_response(value, "research")

    def test_thinking_is_not_treated_as_proposal(self):
        value = response()
        value["response"]["content"].insert(
            0, {"type": "thinking", "thinking": "private"}
        )
        self.assertEqual(parse_response(value, "research").kind, "read")

    def test_multiple_text_blocks_are_rejected(self):
        value = response()
        value["response"]["content"] *= 2
        with self.assertRaises(BackendOutputError):
            parse_response(value, "research")

    def test_invalid_proposal_kinds_are_rejected(self):
        for kind in ([], {}, None, "push", "shell", "publish"):
            with self.subTest(kind=kind), self.assertRaises(BackendOutputError):
                parse_response(response(kind), "research")

    def test_models_require_explicit_claude_identifier(self):
        for model in (
            "sonnet",
            "default",
            "https://evil/model",
            "claude-x\nheader",
            "claude-X",
            "claude-",
            "claude-" + "x" * 200,
            None,
        ):
            with self.subTest(model=model):
                self.assertFalse(valid_model(model))
        self.assertTrue(valid_model("claude-sonnet-5-5"))
        self.assertTrue(valid_model("claude-sonnet-4-20250514"))

    def test_invalid_model_never_fetches_secret(self):
        secret = Mock()
        backend = ClaudeBackend(secret)
        with self.assertRaises(BackendError):
            backend.research(
                BackendRequest(ACCOUNT, "https://evil", "", {}),
                progress=Mock(),
                checkpoint=lambda: True,
                stop=threading.Event(),
            )
        secret.assert_not_called()

    def test_payload_disables_all_tools_without_endpoint_override(self):
        transport = Mock(spec=ProcessTransport)
        transport.exchange.return_value = response()
        backend = ClaudeBackend(grant, transport=transport)
        backend.research(
            BackendRequest(
                ACCOUNT, "claude-sonnet-5-5", "", {"endpoint": "https://evil"}
            ),
            progress=Mock(),
            checkpoint=lambda: True,
            stop=threading.Event(),
        )
        payload = transport.exchange.call_args.args[0]
        self.assertEqual(payload["request"]["tools"], [])
        self.assertEqual(payload["request"]["tool_choice"], {"type": "none"})
        self.assertNotIn("endpoint", payload)

    def test_native_probe_does_not_claim_integration(self):
        result = SimpleNamespace(returncode=0, stdout=b"2.1.118 (Claude Code)\n")
        with patch("coding_runner.claude_backend.subprocess.run", return_value=result):
            value = probe_claude()
        self.assertTrue(value["version_matches"])
        self.assertFalse(value["ready"])
        self.assertFalse(value["native_tools_supported"])

    def test_missing_native_tool_fails_closed(self):
        self.assertFalse(probe_claude("/nonexistent/claude")["version_matches"])


class ClaudeWorkerTests(unittest.TestCase):
    def exchange(self, status=200, body=b"{}", failure=None):
        connection = Mock()
        connection.getresponse.return_value.status = status
        connection.getresponse.return_value.read.return_value = body
        connection.request.side_effect = failure
        payload = {
            "token": TOKEN,
            "timeout": 10,
            "request": {
                "model": "claude-sonnet-5-5",
                "tools": [],
                "tool_choice": {"type": "none"},
            },
        }
        output = io.StringIO()
        incoming = SimpleNamespace(buffer=io.BytesIO(json.dumps(payload).encode()))
        with (
            patch.object(sys, "stdin", incoming),
            redirect_stdout(output),
            patch.object(
                claude_worker.http.client, "HTTPSConnection", return_value=connection
            ) as factory,
        ):
            claude_worker.main()
        return json.loads(output.getvalue()), connection, factory

    def test_worker_uses_pinned_version_and_official_endpoint(self):
        result, connection, factory = self.exchange()
        self.assertEqual(result, {"response": {}})
        factory.assert_called_once_with("api.anthropic.com", timeout=10)
        method, path, body, headers = connection.request.call_args.args
        self.assertEqual((method, path), ("POST", "/v1/messages"))
        self.assertEqual(headers["anthropic-version"], "2026-10-21")
        self.assertEqual(headers["x-api-key"], TOKEN)
        self.assertEqual(json.loads(body)["tools"], [])

    def test_authentication_response_body_is_withheld(self):
        result, connection, _ = self.exchange(401, TOKEN.encode())
        self.assertEqual(result, {"error": "authentication"})
        connection.getresponse.return_value.read.assert_not_called()

    def test_rate_limit_has_no_retry_or_provider_fallback(self):
        result, connection, factory = self.exchange(429, TOKEN.encode())
        self.assertEqual(result, {"error": "model"})
        self.assertEqual(factory.call_count, 1)
        self.assertEqual(connection.request.call_count, 1)

    def test_transport_exceptions_do_not_leak_secret(self):
        result, _, _ = self.exchange(failure=OSError(TOKEN))
        self.assertEqual(result, {"error": "transport"})

    def test_oversized_response_is_withheld(self):
        result, _, _ = self.exchange(body=b"x" * (claude_worker.MAX_BYTES + 1))
        self.assertEqual(result, {"error": "transport"})


class ClaudeProcessTests(unittest.TestCase):
    def backend(self, script):
        return ClaudeBackend(grant, transport=codex_tests.FakeProcess(script))

    def call(self, backend, checkpoint=lambda: True, timeout=1):
        return backend.research(
            BackendRequest(ACCOUNT, "claude-sonnet-5-5", "", {}, timeout),
            progress=Mock(),
            checkpoint=checkpoint,
            stop=threading.Event(),
        )

    def test_real_process_full_adapter_smoke_without_model_call(self):
        backend = self.backend("print(" + repr(json.dumps(response())) + ")")
        self.assertEqual(self.call(backend).kind, "read")
        self.assertIsNone(backend.transport.process)

    def test_worker_has_no_secret_in_environment_arguments_or_cwd(self):
        script = (
            "import json,os,sys; data=json.load(sys.stdin); assert set(os.environ) <= {'LC_CTYPE'}; assert os.getcwd()=='/'; assert data['token'] not in str(sys.argv); print("
            + repr(json.dumps(response()))
            + ")"
        )
        self.assertEqual(self.call(self.backend(script)).kind, "read")

    def test_timeout_terminates_model_process(self):
        backend = self.backend("import time; time.sleep(10)")
        with self.assertRaises(BackendTimeout):
            self.call(backend, timeout=0.05)
        self.assertIsNone(backend.transport.process)

    def test_pause_terminates_model_process(self):
        backend = self.backend("import time; time.sleep(10)")
        polls = iter([True, True, False])
        with self.assertRaises(BackendInterrupted):
            self.call(backend, checkpoint=lambda: next(polls, False))
        self.assertIsNone(backend.transport.process)

    def test_worker_command_is_trusted_and_isolated(self):
        command = ClaudeProcessTransport().command()
        self.assertEqual(command[:2], [sys.executable, "-I"])
        self.assertEqual(Path(command[-1]).name, "claude_worker.py")


class MixedBackendTests(unittest.TestCase):
    def test_each_stage_uses_its_selected_provider_and_model(self):
        effective = {}
        for stage, provider, credential, model in (
            ("research", "codex", 8, "web-codex-model"),
            ("implementation", "claude", 13, "claude-sonnet-5-5"),
        ):
            effective.update(
                {
                    f"{stage}_backend": provider,
                    f"{stage}_credential_id": credential,
                    f"{stage}_model": model,
                    f"{stage}_instructions": stage,
                }
            )
        registry = BackendRegistry()
        research, first = registry.stage(
            {"effective": effective}, "research", ScopedCredentials(Mock())
        )
        implementation, second = registry.stage(
            {"effective": effective}, "implementation", ScopedCredentials(Mock())
        )
        self.assertIsInstance(research, CodexBackend)
        self.assertIsInstance(implementation, ClaudeBackend)
        self.assertEqual(first.model, "web-codex-model")
        self.assertEqual(second.account["credential_id"], 13)

    def evaluate(self, adapters=None):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        journal = Journal(Path(temporary.name) / "journal")
        journal.__enter__()
        self.addCleanup(journal.__exit__)
        config = copy.deepcopy(CONFIG)
        config["global"]["reserve_enabled"] = True
        config["accounts"] = [
            ACCOUNT,
            {"backend": "claude", "credential_id": 14},
            {"backend": "codex", "credential_id": 8},
        ]
        if adapters is None:
            adapters = BackendRegistry().usage_adapters(
                config["accounts"], ScopedCredentials(Mock())
            )
        return GateEvaluator(journal, clock=lambda: NOW, adapters=adapters).evaluate(
            config, journal.load()
        )

    def test_unsupported_claude_quota_blocks_every_project(self):
        result = self.evaluate()
        self.assertFalse(result.allowed)
        self.assertEqual(
            {(row["backend"], row["credential_id"]) for row in result.readings},
            {("claude", 13), ("claude", 14), ("codex", 8)},
        )

    def test_accounts_are_not_averaged(self):
        high = FakeUsage((UsageWindow("daily", 100, NOW),))
        low = FakeUsage((UsageWindow("daily", 39, NOW),))
        result = self.evaluate(
            {("claude", 13): low, ("claude", 14): high, ("codex", 8): high}
        )
        self.assertFalse(result.allowed)
        self.assertIn("usage_reserve", result.reasons)

    def test_stale_claude_reading_does_not_use_fresh_codex_allowance(self):
        high = FakeUsage((UsageWindow("daily", 100, NOW),))
        stale = FakeUsage((UsageWindow("daily", 100, NOW - timedelta(seconds=300)),))
        result = self.evaluate(
            {("claude", 13): stale, ("claude", 14): high, ("codex", 8): high}
        )
        self.assertFalse(result.allowed)
        self.assertIn("usage_stale", result.reasons)

    def test_unavailable_second_claude_account_blocks_global_reserve(self):
        high = FakeUsage((UsageWindow("daily", 100, NOW),))
        result = self.evaluate({("claude", 13): high, ("codex", 8): high})
        self.assertFalse(result.allowed)
        self.assertIn("usage_missing", result.reasons)


class ClaudeApprovalRecoveryTests(unittest.TestCase):
    step = codex_tests.ResearchTests.step

    def setUp(self):
        codex_tests.ResearchTests.setUp(self)
        self.transport = Mock(spec=ProcessTransport)
        self.transport.exchange.return_value = response()
        self.backend = ClaudeBackend(grant, transport=self.transport)
        self.backend.readiness = Mock(
            return_value=BackendReadiness(True, True, (), CAPABILITIES)
        )
        self.stage.backend = self.backend
        self.request = BackendRequest(ACCOUNT, "claude-sonnet-5-5", "instructions", {})

    def action(self):
        self.step()
        observed = self.state["research"][self.run_id]["observations"][0]
        action = ActionSpec(
            ("/usr/bin/true",),
            "exec",
            "repository",
            "app.py",
            observed["digest"],
            self.run_id,
            codex_tests.BASE,
            self.workspace.verify(),
        )
        self.transport.exchange.return_value = response(
            "action", {"action": action.to_data()}
        )
        return action

    def test_individual_human_approval_retains_claim_across_restart(self):
        self.action()
        self.assertEqual(self.step().status, "approval")
        self.state = self.journal.load()
        self.assertEqual(self.step().status, "approval")
        self.assertEqual(self.transport.exchange.call_count, 2)
        self.executor.execute.assert_not_called()

    def test_only_approved_bound_command_reaches_executor(self):
        action = self.action()
        self.executor.broker.request.return_value["decision"] = "approved"
        self.executor.execute.return_value = SimpleNamespace(
            execution_id=str(uuid4()),
            exit_code=0,
            output=b"inspected",
            interrupted=False,
        )
        self.assertEqual(self.step().status, "continue")
        self.assertEqual(self.executor.execute.call_args.args[1], action)

    def test_changed_source_invalidates_claude_action(self):
        self.action()
        (self.workspace.checkout / "app.py").write_text("changed")
        with self.assertRaises(ApprovalRequired):
            self.step()
        self.executor.execute.assert_not_called()

    def test_ambiguous_model_outcome_needs_operator_recovery(self):
        self.transport.exchange.side_effect = BackendTimeout("unknown")
        with self.assertRaises(BackendTimeout):
            self.step()
        self.state = self.journal.load()
        with self.assertRaisesRegex(BackendError, "unresolved"):
            self.step()
        self.assertEqual(self.transport.exchange.call_count, 1)
