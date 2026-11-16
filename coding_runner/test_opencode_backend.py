import copy
import io
import json
import socket
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import uuid4

from . import opencode_worker, test_backends as codex_tests
from .approvals import ActionSpec, ApprovalRequired
from .backend_registry import BackendRegistry, ScopedCredentials
from .backends import (
    BackendError,
    BackendInterrupted,
    BackendOutputError,
    BackendReadiness,
    BackendRequest,
    BackendTimeout,
)
from .claude_backend import ClaudeBackend
from .codex_backend import CodexBackend
from .gates import GateEvaluator, UsageWindow
from .journal import Journal
from .opencode_backend import (
    CAPABILITIES,
    OpenCodeBackend,
    OpenCodeProcessTransport,
    parse_response,
    probe_opencode,
)
from .opencode_connection import OFFICIAL_ENDPOINTS, validate_connection, valid_model
from .proposal_backend import ProcessTransport
from .test_claude_backend import AdapterContract
from .test_gates import CONFIG, NOW, FakeUsage

ACCOUNT = {"backend": "opencode", "credential_id": 14}
TOKEN = "selected-claude-secret"


def grant(account, provider="ollama", endpoint="http://runner-ollama:11434"):
    connection = {"provider": provider, "api_key": TOKEN}
    if provider == "ollama":
        connection["base_url"] = endpoint
    return {
        "credential": {
            "id": account["credential_id"],
            "provider": account["backend"],
            "kind": "connection",
            "version": 1,
        },
        "material": {"secret": json.dumps(connection), "passphrase": ""},
    }


def response(kind="read", data=None, provider="ollama"):
    content = json.dumps(
        {"kind": kind, "data": {"path": "app.py"} if data is None else data}
    )
    message = {"role": "assistant", "content": content}
    if provider == "ollama":
        return {
            "response": {
                "model": "qwen3-coder:30b",
                "message": message,
                "done": True,
                "done_reason": "stop",
            }
        }
    return {
        "response": {
            "model": "qwen3-coder:30b",
            "choices": [{"message": message, "finish_reason": "stop"}],
        }
    }


class OpenCodeContractTests(AdapterContract, unittest.TestCase):
    provider = "opencode"

    def setUp(self):
        self.account = ACCOUNT
        self.transport = Mock(spec=ProcessTransport)
        self.transport.exchange.return_value = response()
        self.backend = OpenCodeBackend(grant, transport=self.transport)
        self.request = BackendRequest(
            ACCOUNT, "qwen3-coder:30b", TOKEN, {"source": TOKEN}
        )
        self.progress = Mock()
        self.envelope = response

    def test_ollama_has_no_tools_and_requires_json(self):
        self.run_stage()
        body = self.transport.exchange.call_args.args[0]["request"]
        self.assertEqual(body["tools"], [])
        self.assertEqual(body["format"], "json")
        self.assertFalse(body["stream"])
        self.assertEqual(body["model"], "qwen3-coder:30b")

    def test_hosted_chat_has_explicit_no_tool_choice(self):
        for provider in ("openai", "opencode"):
            with self.subTest(provider=provider):
                self.backend.credentials = lambda account: grant(account, provider)
                self.transport.exchange.return_value = response(provider=provider)
                self.run_stage()
                body = self.transport.exchange.call_args.args[0]["request"]
                self.assertEqual(body["tools"], [])
                self.assertEqual(body["tool_choice"], "none")

    def test_anthropic_uses_separate_messages_semantics(self):
        from .test_claude_backend import response as anthropic_response

        self.backend.credentials = lambda account: grant(account, "anthropic")
        self.transport.exchange.return_value = anthropic_response()
        self.transport.exchange.return_value["response"]["model"] = self.request.model
        self.run_stage()
        body = self.transport.exchange.call_args.args[0]["request"]
        self.assertEqual(body["tools"], [])
        self.assertEqual(body["tool_choice"], {"type": "none"})
        self.assertIn("system", body)
        self.assertEqual(len(body["messages"]), 1)

    def test_invalid_model_blocks_before_secret_delivery(self):
        self.backend.credentials = Mock()
        self.request = replace(self.request, model="https://malicious.example/model")
        with self.assertRaises(BackendError):
            self.run_stage()
        self.backend.credentials.assert_not_called()

    def test_invalid_timeout_blocks_before_secret_delivery(self):
        for timeout in (0, -1, 86401, float("nan")):
            self.request = replace(self.request, timeout_seconds=timeout)
            with self.subTest(timeout=timeout), self.assertRaises(BackendError):
                self.run_stage()
        self.transport.exchange.assert_not_called()

    def test_local_connection_without_key_is_supported(self):
        selected = grant(ACCOUNT)
        value = json.loads(selected["material"]["secret"])
        del value["api_key"]
        selected["material"]["secret"] = json.dumps(value)
        self.backend.credentials = lambda account: selected
        self.assertEqual(self.run_stage().kind, "read")
        self.assertIn(
            "Return one JSON",
            self.transport.exchange.call_args.args[0]["request"]["messages"][0][
                "content"
            ],
        )


class ConnectionTests(unittest.TestCase):
    def test_fixed_hosted_endpoints(self):
        for provider, endpoint in OFFICIAL_ENDPOINTS.items():
            with self.subTest(provider=provider):
                self.assertEqual(
                    validate_connection({"provider": provider, "api_key": TOKEN})[
                        "base_url"
                    ],
                    endpoint,
                )
                with self.assertRaises(ValueError):
                    validate_connection(
                        {
                            "provider": provider,
                            "api_key": TOKEN,
                            "base_url": "https://steal.example/v1",
                        }
                    )

    def test_local_endpoints_are_explicit(self):
        for endpoint in (
            "http://runner-ollama:11434",
            "https://127.0.0.1:123/api",
            "http://[::1]:11434/",
            "http://192.168.1.42:11434/api/",
        ):
            with self.subTest(endpoint=endpoint):
                self.assertTrue(
                    validate_connection({"provider": "ollama", "base_url": endpoint})[
                        "base_url"
                    ].endswith("/api")
                )

    def test_invalid_endpoints_are_rejected(self):
        endpoints = [
            "file:///secret",
            "http://user:pass@127.0.0.1",
            "http://127.0.0.1?key=secret",
            "http://127.0.0.1#fragment",
            "http://127.0.0.1:99999",
            "http://127.0.0.1:0",
            "http://127.0.0.1/v1",
            "http://169.254.169.254",
            "http://0.0.0.0",
            "http://8.8.8.8",
            "http://unknown.internal",
            "https://example.com",
            "http://127.0.0.1/ api",
        ]
        for endpoint in endpoints:
            with self.subTest(endpoint=endpoint), self.assertRaises(ValueError):
                validate_connection({"provider": "ollama", "base_url": endpoint})

    def test_missing_endpoint_or_key_and_unknown_fields_are_rejected(self):
        for value in (
            {"provider": "ollama"},
            {"provider": "openai"},
            {"provider": "unknown"},
            {"provider": "ollama", "base_url": "http://localhost", "headers": {}},
            {"provider": "openai", "api_key": "bad\nheader"},
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_connection(value)

    def test_model_ids(self):
        for model in (
            "qwen3-coder:30b",
            "namespace/model:latest",
            "gpt-5",
            "claude-sonnet-5-5",
        ):
            self.assertTrue(valid_model(model))
        for model in (
            "",
            "https://host/model",
            "../secret",
            "model --auto",
            "model\ncommand",
            "a" * 201,
        ):
            self.assertFalse(valid_model(model))

    def test_matching_native_version_still_cannot_grant_execution(self):
        result = SimpleNamespace(returncode=0, stdout=b"1.19.5\n")
        with patch(
            "coding_runner.opencode_backend.subprocess.run", return_value=result
        ) as probe:
            value = probe_opencode("/trusted/opencode")
        self.assertTrue(value["version_matches"])
        self.assertFalse(value["ready"])
        self.assertFalse(value["native_tools_supported"])
        self.assertEqual(probe.call_args.args[0], ["/trusted/opencode", "--version"])

    def test_native_cli_probe_never_grants_execution(self):
        result = probe_opencode("/tmp/does-not-exist-opencode")
        self.assertFalse(result["ready"])
        self.assertFalse(result["native_tools_supported"])


class SchemaTests(unittest.TestCase):
    def test_native_tools_are_rejected(self):
        for provider in ("ollama", "openai", "opencode"):
            for key in ("tool_calls", "function_call", "refusal"):
                value = response(provider=provider)
                message = (
                    value["response"].get("message")
                    or value["response"]["choices"][0]["message"]
                )
                message[key] = [{"name": "shell"}]
                with (
                    self.subTest(provider=provider, key=key),
                    self.assertRaises(BackendOutputError),
                ):
                    parse_response(value, "research", provider)

    def test_partial_or_multiple_outputs_are_rejected(self):
        cases = [
            ({"response": {"done": False, "message": {}}}, "ollama"),
            (
                {"response": {"done": True, "done_reason": "length", "message": {}}},
                "ollama",
            ),
            ({"response": {"choices": []}}, "openai"),
        ]
        for value, provider in cases:
            with self.subTest(provider=provider), self.assertRaises(BackendOutputError):
                parse_response(value, "research", provider)

    def test_unknown_actions_and_unsafe_reads_are_rejected(self):
        for kind, data in (
            ("shell", {"command": "true"}),
            ("read", {"path": "../secret"}),
            ("read", {"path": ".git/config"}),
            ("complete", {"summary": "done"}),
        ):
            with self.subTest(kind=kind), self.assertRaises(BackendOutputError):
                parse_response(response(kind, data), "research", "ollama")


class InferenceServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), InferenceHandler)
        self.calls = []
        self.status = 200
        self.delay = 0
        self.value = response()["response"]


class InferenceHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.server.calls.append((self.path, dict(self.headers), None))
        self.send_response(self.server.status)
        self.end_headers()
        self.wfile.write(json.dumps(self.server.value).encode())

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.calls.append((self.path, dict(self.headers), body))
        time.sleep(self.server.delay)
        self.send_response(self.server.status)
        self.send_header("Location", "http://127.0.0.1:1/credential-theft")
        self.end_headers()
        try:
            self.wfile.write(json.dumps(self.server.value).encode())
        except BrokenPipeError:
            pass


class WorkerHTTPTests(unittest.TestCase):
    def setUp(self):
        self.server = InferenceServer()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.endpoint = "http://127.0.0.1:" + str(self.server.server_port)
        self.backend = OpenCodeBackend(
            lambda account: grant(account, endpoint=self.endpoint)
        )
        self.request = BackendRequest(ACCOUNT, "qwen3-coder:30b", "instructions", {})

    def run_model(self, **kwargs):
        return self.backend.research(
            self.request,
            progress=lambda event: None,
            checkpoint=kwargs.get("checkpoint", lambda: True),
            stop=threading.Event(),
        )

    def test_actual_private_stdin_worker_sends_only_toolless_request(self):
        self.assertEqual(self.run_model().kind, "read")
        path, headers, body = self.server.calls[0]
        self.assertEqual(path, "/api/chat")
        self.assertEqual(headers["Authorization"], "Bearer " + TOKEN)
        self.assertNotIn(TOKEN, json.dumps(body))
        self.assertEqual(body["tools"], [])

    def test_redirect_does_not_forward_credentials(self):
        self.server.status = 307
        with self.assertRaises(BackendError):
            self.run_model()
        self.assertEqual(len(self.server.calls), 1)

    def test_authentication_body_is_withheld(self):
        self.server.status, self.server.value = 401, {"secret": TOKEN}
        with self.assertRaises(BackendError) as result:
            self.run_model()
        self.assertNotIn(TOKEN, str(result.exception))

    def test_unreachable_inference_fails_closed(self):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        self.backend.credentials = lambda account: grant(
            account, endpoint="http://127.0.0.1:" + str(port)
        )
        with self.assertRaises(BackendError):
            self.run_model()

    def test_timeout_stops_real_worker(self):
        self.server.delay = 1
        self.request = replace(self.request, timeout_seconds=0.1)
        with self.assertRaises(BackendTimeout):
            self.run_model()
        self.assertIsNone(self.backend.transport.process)

    def test_pause_stops_real_worker(self):
        self.server.delay = 1
        calls = 0

        def checkpoint():
            nonlocal calls
            calls += 1
            return calls < 4

        with self.assertRaises(BackendInterrupted):
            self.run_model(checkpoint=checkpoint)
        self.assertIsNone(self.backend.transport.process)

    def test_nonbillable_probe_requires_installed_model(self):
        self.server.value = {"models": [{"name": "qwen3-coder:30b"}]}
        account = {**ACCOUNT, "model": "qwen3-coder:30b"}
        self.assertTrue(self.backend.probe_authentication(account))
        self.assertEqual(self.server.calls[0][0], "/api/tags")
        self.assertIsNone(self.server.calls[0][2])
        self.assertFalse(
            self.backend.probe_authentication({**account, "model": "missing:latest"})
        )

    def test_nonbillable_probe_rejects_failed_authentication(self):
        self.server.status = 401
        self.assertFalse(
            self.backend.probe_authentication({**ACCOUNT, "model": "qwen3-coder:30b"})
        )

    def test_worker_command_is_trusted_and_isolated(self):
        command = OpenCodeProcessTransport().command()
        self.assertEqual(command[1], "-I")
        self.assertEqual(Path(command[-1]).name, "opencode_worker.py")


class OpenCodeApprovalRecoveryTests(unittest.TestCase):
    step = codex_tests.ResearchTests.step

    def setUp(self):
        codex_tests.ResearchTests.setUp(self)
        self.transport = Mock(spec=ProcessTransport)
        self.transport.exchange.return_value = response()
        self.backend = OpenCodeBackend(grant, transport=self.transport)
        self.backend.readiness = Mock(
            return_value=BackendReadiness(True, True, (), CAPABILITIES)
        )
        self.stage.backend = self.backend
        self.request = BackendRequest(ACCOUNT, "qwen3-coder:30b", "instructions", {})

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

    def test_human_approval_wait_retains_claim_on_restart(self):
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

    def test_source_change_invalidates_action(self):
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


class MixedBackendTests(unittest.TestCase):
    def test_stages_preserve_backend_model_and_account(self):
        registry = BackendRegistry()
        for first, second in (("codex", "opencode"), ("opencode", "claude")):
            effective = {}
            for stage, backend, credential, model in (
                ("research", first, 8, "qwen3-coder:30b"),
                ("implementation", second, 14, "claude-sonnet-5-5"),
            ):
                effective.update(
                    {
                        f"{stage}_backend": backend,
                        f"{stage}_credential_id": credential,
                        f"{stage}_model": model,
                        f"{stage}_instructions": stage,
                    }
                )
            first_backend, first_request = registry.stage(
                {"effective": effective}, "research", ScopedCredentials(Mock())
            )
            second_backend, second_request = registry.stage(
                {"effective": effective}, "implementation", ScopedCredentials(Mock())
            )
            self.assertEqual(
                type(first_backend),
                {"codex": CodexBackend, "opencode": OpenCodeBackend}[first],
            )
            self.assertEqual(
                type(second_backend),
                {"claude": ClaudeBackend, "opencode": OpenCodeBackend}[second],
            )
            self.assertEqual(first_request.account["credential_id"], 8)
            self.assertEqual(second_request.account["credential_id"], 14)
            self.assertEqual(first_request.model, "qwen3-coder:30b")

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
            {"backend": "opencode", "credential_id": 15},
            {"backend": "claude", "credential_id": 13},
        ]
        if adapters is None:
            adapters = BackendRegistry().usage_adapters(
                config["accounts"], ScopedCredentials(Mock())
            )
        return GateEvaluator(journal, clock=lambda: NOW, adapters=adapters).evaluate(
            config, journal.load()
        )

    def test_local_ollama_is_not_unlimited_allowance(self):
        decision = self.evaluate()
        self.assertFalse(decision.allowed)
        self.assertEqual(
            {(row["backend"], row["credential_id"]) for row in decision.readings},
            {("opencode", 14), ("opencode", 15), ("claude", 13)},
        )

    def test_second_account_cannot_borrow_allowance(self):
        high = FakeUsage((UsageWindow("daily", 100, NOW),))
        self.assertFalse(
            self.evaluate({("opencode", 14): high, ("claude", 13): high}).allowed
        )


class HostedWorkerTests(unittest.TestCase):
    def test_fixed_hosts_paths_and_provider_authentication(self):
        for provider, host, path in (
            ("openai", "api.openai.com", "/v1/chat/completions"),
            ("anthropic", "api.anthropic.com", "/v1/messages"),
            ("opencode", "opencode.ai", "/inference/openai/v1/chat/completions"),
        ):
            with self.subTest(provider=provider):
                incoming = {
                    "connection": {"provider": provider, "api_key": TOKEN},
                    "timeout": 3,
                    "request": {"tools": []},
                }
                connection = Mock()
                connection.getresponse.return_value = SimpleNamespace(
                    status=200, read=lambda size: b"{}"
                )
                output = io.StringIO()
                with (
                    patch.object(
                        opencode_worker.sys,
                        "stdin",
                        SimpleNamespace(
                            buffer=io.BytesIO(json.dumps(incoming).encode())
                        ),
                    ),
                    patch.object(opencode_worker.sys, "stdout", output),
                    patch.object(
                        opencode_worker.http.client,
                        "HTTPSConnection",
                        return_value=connection,
                    ) as factory,
                ):
                    opencode_worker.main()
                factory.assert_called_once_with(host, None, timeout=3)
                args = connection.request.call_args.args
                self.assertEqual(args[0:2], ("POST", path))
                headers = args[3]
                if provider == "anthropic":
                    self.assertEqual(headers["x-api-key"], TOKEN)
                    self.assertNotIn("Authorization", headers)
                else:
                    self.assertEqual(headers["Authorization"], "Bearer " + TOKEN)
                self.assertNotIn(TOKEN, output.getvalue())
                connection.close.assert_called_once_with()

    def test_worker_revalidates_destination_before_sending_secrets(self):
        incoming = {
            "connection": {
                "provider": "openai",
                "api_key": TOKEN,
                "base_url": "https://steal.example/v1",
            },
            "timeout": 3,
            "request": {},
        }
        output = io.StringIO()
        with (
            patch.object(
                opencode_worker.sys,
                "stdin",
                SimpleNamespace(buffer=io.BytesIO(json.dumps(incoming).encode())),
            ),
            patch.object(opencode_worker.sys, "stdout", output),
            patch.object(opencode_worker.http.client, "HTTPSConnection") as factory,
        ):
            opencode_worker.main()
        factory.assert_not_called()
        self.assertEqual(json.loads(output.getvalue()), {"error": "transport"})


class OpenCodeQualificationTests(unittest.TestCase):
    def setUp(self):
        from .backend_transports import PROOF_PROTOCOL, TRANSPORTS

        self.account = {**ACCOUNT, "model": "qwen3-coder:30b"}
        self.entry = {
            **self.account,
            "secret_version": 1,
            "transport": TRANSPORTS["opencode"],
        }
        self.proof = {
            "qualified": True,
            "transport": PROOF_PROTOCOL,
            "accounts": [self.entry],
        }
        self.credentials = Mock(side_effect=grant)
        self.credentials.qualification.return_value = {
            **self.entry,
            "verification": self.proof,
        }
        self.backend = OpenCodeBackend(self.credentials)

    def readiness(self, deployment=True):
        with patch("coding_runner.release.matches_deployment", return_value=deployment):
            return self.backend.readiness(self.account)

    def test_exact_qualified_deployment_can_enable_candidate(self):
        self.assertTrue(self.readiness().ready)
        self.assertTrue(self.readiness().authenticated)

    def test_other_transport_account_model_and_version_are_rejected(self):
        for key, value in (
            ("transport", "opencode-native"),
            ("transport", "claude-messages-structured-proposals-v1"),
            ("credential_id", 99),
            ("model", "another-model"),
            ("backend", "claude"),
            ("secret_version", 2),
        ):
            self.credentials.qualification.return_value = {
                **self.entry,
                key: value,
                "verification": self.proof,
            }
            with self.subTest(key=key, value=value):
                self.assertFalse(self.readiness().ready)

    def test_changed_endpoint_requires_new_connection_version_and_proof(self):
        changed = grant(ACCOUNT, endpoint="http://127.0.0.1:11435")
        changed["credential"]["version"] = 2
        self.credentials.side_effect = lambda account: changed
        self.assertFalse(self.readiness().ready)

    def test_expired_or_changed_deployment_blocks(self):
        self.assertFalse(self.readiness(False).ready)

    def test_exact_account_must_appear_in_proof(self):
        self.proof["accounts"] = []
        self.assertFalse(self.readiness().ready)


class OpenCodeTrackedWorkerTests(unittest.TestCase):
    run_model = WorkerHTTPTests.run_model

    def setUp(self):
        from .processes import ProcessTracker, current_tracker

        WorkerHTTPTests.setUp(self)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.journal = Journal(Path(temporary.name) / "journal")
        self.journal.__enter__()
        self.addCleanup(self.journal.__exit__)
        self.state = self.journal.load()
        self.tracker = ProcessTracker(self.journal, self.state)
        token = current_tracker.set(self.tracker)
        self.addCleanup(current_tracker.reset, token)

    def assert_stopped(self):
        rows = self.journal.load()["processes"]
        self.assertEqual(len(rows), 1)
        row = next(iter(rows.values()))
        self.assertEqual(row["status"], "stopped")
        self.assertTrue(row["pid"])
        self.assertTrue(row["start"])
        self.assertNotIn(TOKEN, json.dumps(rows))
        self.tracker.recover()

    def test_actual_worker_is_durably_tracked_and_reaped(self):
        self.assertEqual(self.run_model().kind, "read")
        self.assert_stopped()

    def test_actual_worker_timeout_is_stopped_before_recovery(self):
        self.server.delay = 1
        self.request = replace(self.request, timeout_seconds=0.1)
        with self.assertRaises(BackendTimeout):
            self.run_model()
        self.assert_stopped()

    def test_actual_worker_pause_is_stopped_before_recovery(self):
        self.server.delay = 1
        calls = 0

        def checkpoint():
            nonlocal calls
            calls += 1
            return calls < 4

        with self.assertRaises(BackendInterrupted):
            self.run_model(checkpoint=checkpoint)
        self.assert_stopped()

    def test_actual_worker_receipt_hashes_raw_http_bytes(self):
        import hashlib

        selected = json.loads(
            grant(ACCOUNT, endpoint=self.endpoint)["material"]["secret"]
        )
        value = self.backend.transport.exchange(
            {
                "connection": selected,
                "timeout": 3,
                "request": {"model": self.request.model, "messages": [], "tools": []},
            },
            timeout=3,
            checkpoint=lambda: True,
            stop=threading.Event(),
        )
        raw = json.dumps(self.server.value).encode()
        self.assertEqual(
            value["receipt"]["response_digest"], hashlib.sha256(raw).hexdigest()
        )
        self.assertNotIn("response_id", value["receipt"])
        self.assert_stopped()


class OpenCodeTraceTests(unittest.TestCase):
    def test_local_receipt_is_typed_and_bound_without_invented_provider_id(self):
        from .qualification_trace import ModelTraceTransport

        selected = validate_connection(json.loads(grant(ACCOUNT)["material"]["secret"]))
        account = {**ACCOUNT, "model": "qwen3-coder:30b"}
        state = {}
        value = {
            **response(),
            "receipt": {"response_digest": "a" * 64, "ollama_version": "0.40.16"},
        }
        transport = ModelTraceTransport(Mock(), state, account, 2)
        with patch.object(ProcessTransport, "exchange", return_value=value):
            transport.exchange(
                {"connection": selected, "request": {"model": account["model"]}}
            )
        trace = state["release_evidence"]["model_responses"][0]
        self.assertEqual(trace["receipt_kind"], "local_response_digest")
        self.assertEqual(trace["response_digest"], "a" * 64)
        self.assertEqual(trace["endpoint"], "http://runner-ollama:11434/api")
        self.assertEqual(trace["secret_version"], 2)
        self.assertEqual(trace["ollama_version"], "0.40.16")
        self.assertNotIn("response_id", trace)
        self.assertNotIn(TOKEN, json.dumps(trace))
        self.assertEqual(Path(transport.command()[-1]).name, "opencode_worker.py")

    def test_untyped_or_native_tool_output_cannot_supply_local_receipt(self):
        from .qualification_trace import ModelTraceTransport

        selected = validate_connection(json.loads(grant(ACCOUNT)["material"]["secret"]))
        cases = [
            response(),
            {
                **response("shell", {"command": "true"}),
                "receipt": {"response_digest": "a" * 64},
            },
        ]
        for value in cases:
            state = {}
            transport = ModelTraceTransport(Mock(), state, ACCOUNT, 1)
            with (
                patch.object(ProcessTransport, "exchange", return_value=value),
                self.assertRaises(BackendError),
            ):
                transport.exchange(
                    {"connection": selected, "request": {"model": "qwen3-coder:30b"}}
                )
            self.assertNotIn("release_evidence", state)

    def test_wrong_reported_model_cannot_supply_local_receipt(self):
        from .qualification_trace import ModelTraceTransport

        value = {**response(), "receipt": {"response_digest": "a" * 64}}
        value["response"]["model"] = "wrong-model"
        state = {}
        transport = ModelTraceTransport(Mock(), state, ACCOUNT, 1)
        with (
            patch.object(ProcessTransport, "exchange", return_value=value),
            self.assertRaises(BackendOutputError),
        ):
            transport.exchange(
                {
                    "connection": {"provider": "ollama"},
                    "request": {"model": "qwen3-coder:30b"},
                }
            )
        self.assertNotIn("release_evidence", state)
