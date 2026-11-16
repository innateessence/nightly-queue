import copy
import io
import json
import os
import signal
import sys
import tempfile
import threading
import unittest
from contextlib import nullcontext, redirect_stdout
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import uuid4

from .approvals import ActionSpec, ApprovalRequired
from .backend_registry import BackendRegistry, ScopedCredentials
from .backends import (
    BackendAuthenticationError,
    BackendError,
    BackendEvent,
    BackendInterrupted,
    BackendOutputError,
    BackendReadiness,
    BackendRequest,
    BackendTimeout,
)
from .codex_backend import (
    CAPABILITIES,
    CodexBackend,
    ProcessTransport,
    parse_response,
    probe_codex,
    validate_proposal,
)
from .context import digest, manual_context
from .gates import GateEvaluator
from .journal import Journal
from .research import ResearchStage, read_checkout
from .model_worker import main as worker_main
from .test_gates import CONFIG, NOW
from .workspaces import SourceStore, content_digest

ACCOUNT = {"backend": "codex", "credential_id": 7}
TOKEN = "private-selected-token"
BASE = "a" * 40


def grant(account):
    return {
        "credential": {
            "id": account["credential_id"],
            "provider": "codex",
            "kind": "token",
            "version": 1,
        },
        "material": {"secret": TOKEN, "passphrase": ""},
    }


def response(kind="read", data=None):
    proposal = {"kind": kind, "data": data if data is not None else {"path": "app.py"}}
    return {
        "response": {
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": json.dumps(proposal)}],
                }
            ],
        }
    }


class FakeProcess(ProcessTransport):
    def __init__(self, script):
        super().__init__()
        self.script = script

    def command(self):
        return [sys.executable, "-I", "-c", self.script]


class BackendTests(unittest.TestCase):
    def setUp(self):
        self.transport = Mock(spec=ProcessTransport)
        self.transport.exchange.return_value = response()
        self.backend = CodexBackend(grant, transport=self.transport)
        self.request = BackendRequest(
            ACCOUNT, "web-selected-model", "Web instructions", {}
        )
        self.progress = Mock()
        self.stop = threading.Event()

    def research(self):
        return self.backend.research(
            self.request,
            progress=self.progress,
            checkpoint=lambda: True,
            stop=self.stop,
        )

    def test_model_selection_and_token_stay_out_of_prompt(self):
        self.assertEqual(self.research().kind, "read")
        payload = self.transport.exchange.call_args.args[0]
        self.assertEqual(payload["request"]["model"], "web-selected-model")
        self.assertIn("Web instructions", payload["request"]["instructions"])
        self.assertNotIn(TOKEN, json.dumps(payload["request"]))
        self.assertNotIn(TOKEN, str(self.progress.call_args_list))

    def test_no_tools_or_persistence_are_requested(self):
        self.research()
        payload = self.transport.exchange.call_args.args[0]["request"]
        self.assertEqual(payload["tools"], [])
        self.assertEqual(payload["tool_choice"], "none")
        self.assertIs(payload["store"], False)

    def test_selected_secret_is_redacted_even_if_source_contains_it(self):
        self.request = BackendRequest(ACCOUNT, "model", TOKEN, {"source": TOKEN})
        self.research()
        self.assertNotIn(
            TOKEN, json.dumps(self.transport.exchange.call_args.args[0]["request"])
        )

    def test_json_escaped_secret_is_removed_before_sending_source(self):
        token = 'private-"quoted\\unicode-é-token'
        material = grant(ACCOUNT)
        material["material"]["secret"] = token
        self.backend.credentials = lambda account: material
        self.request = BackendRequest(ACCOUNT, "model", token, {token: token})
        self.research()
        payload = self.transport.exchange.call_args.args[0]["request"]
        self.assertNotIn(token, payload["instructions"])
        self.assertEqual(json.loads(payload["input"]), {"[redacted]": "[redacted]"})

    def test_secret_in_model_selection_is_not_sent(self):
        self.request = BackendRequest(ACCOUNT, TOKEN, "", {})
        with self.assertRaises(BackendError):
            self.research()
        self.transport.exchange.assert_not_called()

    def test_provider_cannot_return_selected_secret_as_proposal_data(self):
        self.transport.exchange.return_value = response("read", {"path": TOKEN})
        with self.assertRaises(BackendOutputError) as failure:
            self.research()
        self.assertNotIn(TOKEN, str(failure.exception))

    def test_provider_cannot_return_json_escaped_secret_as_proposal_data(self):
        token = 'private-"quoted\\unicode-é-token'
        material = grant(ACCOUNT)
        material["material"]["secret"] = token
        self.backend.credentials = lambda account: material
        self.transport.exchange.return_value = response("read", {"path": token})
        with self.assertRaises(BackendOutputError):
            self.research()

    def test_authentication_failure_is_redacted(self):
        self.transport.exchange.return_value = {"error": "authentication"}
        with self.assertRaises(BackendAuthenticationError) as failure:
            self.research()
        self.assertNotIn(TOKEN, str(failure.exception))

    def test_wrong_account_cannot_select_other_credential(self):
        self.backend.credentials = lambda account: grant({"credential_id": 99})
        with self.assertRaises(BackendAuthenticationError):
            self.research()
        self.transport.exchange.assert_not_called()

    def test_session_material_is_explicitly_unsupported(self):
        material = grant(ACCOUNT)
        material["credential"]["kind"] = "session"
        self.backend.credentials = lambda account: material
        self.assertFalse(self.backend.readiness(ACCOUNT).ready)

    def test_structural_credentials_do_not_claim_live_authentication(self):
        readiness = self.backend.readiness(ACCOUNT)
        self.assertFalse(readiness.ready)
        self.assertFalse(readiness.authenticated)

    def test_usage_does_not_invent_remaining_quota_from_tokens(self):
        self.assertFalse(self.backend.read_usage(ACCOUNT, datetime.now(UTC)).supported)
        self.transport.exchange.assert_not_called()

    def test_shutdown_prevents_transport(self):
        self.stop.set()
        with self.assertRaises(BackendInterrupted):
            self.research()
        self.transport.exchange.assert_not_called()

    def test_pause_prevents_transport(self):
        with self.assertRaises(BackendInterrupted):
            self.backend.research(
                self.request,
                progress=self.progress,
                checkpoint=lambda: False,
                stop=self.stop,
            )
        self.transport.exchange.assert_not_called()

    def test_model_error_does_not_expose_provider_body(self):
        self.transport.exchange.return_value = {"error": TOKEN}
        with self.assertRaises(BackendError) as failure:
            self.research()
        self.assertNotIn(TOKEN, str(failure.exception))

    def test_research_rejects_implementation_and_publish_events(self):
        for kind in (
            "patch",
            "complete",
            "implementation",
            "publish",
            "push",
            "amendment",
        ):
            with self.subTest(kind=kind), self.assertRaises(BackendOutputError):
                parse_response(response(kind, {}), "research")

    def test_rejects_every_native_tool_event(self):
        for kind in (
            "function_call",
            "shell_call",
            "custom_tool_call",
            "web_search_call",
            "computer_call",
            "file_search_call",
            "tool_output",
        ):
            value = response()
            value["response"]["output"].append({"type": kind})
            with self.subTest(kind=kind), self.assertRaises(BackendOutputError):
                parse_response(value, "research")

    def test_refusal_does_not_become_a_plan(self):
        value = response()
        value["response"]["output"][0]["content"][0] = {
            "type": "refusal",
            "refusal": TOKEN,
        }
        with self.assertRaises(BackendOutputError):
            parse_response(value, "research")

    def test_malformed_or_incomplete_outputs_are_rejected(self):
        values = [
            {},
            {"response": {}},
            {"response": {"status": "incomplete"}},
            response("read", {"path": "../escape"}),
            response([], {}),
        ]
        for value in values:
            with self.subTest(value=value), self.assertRaises(BackendOutputError):
                parse_response(value, "research")

    def test_implementation_patch_binds_expected_file_and_checkout(self):
        data = {
            "base_revision": BASE,
            "content_digest": "b" * 64,
            "files": [
                {
                    "path": "app.py",
                    "expected_digest": "c" * 64,
                    "content": "replacement",
                }
            ],
        }
        self.assertEqual(
            parse_response(response("patch", data), "implementation").data, data
        )

    def test_implementation_patch_rejects_metadata_and_duplicate_paths(self):
        for path in (".git/config", "/etc/passwd", "../other", "a//b"):
            data = {
                "base_revision": BASE,
                "content_digest": "b" * 64,
                "files": [
                    {"path": path, "expected_digest": None, "content": "replacement"}
                ],
            }
            with self.subTest(path=path), self.assertRaises(BackendOutputError):
                validate_proposal("patch", data)

    def test_unsupported_native_cli_never_reports_readiness(self):
        result = SimpleNamespace(returncode=0, stdout=b"codex-cli 0.169.0\n")
        with patch("coding_runner.codex_backend.subprocess.run", return_value=result):
            probe = probe_codex()
        self.assertTrue(probe["version_matches"])
        self.assertFalse(probe["ready"])


class TransportTests(unittest.TestCase):
    def exchange(self, script, *, checkpoint=lambda: True, stop=None, timeout=1):
        transport = FakeProcess(script)
        result = transport.exchange(
            {"token": TOKEN},
            timeout=timeout,
            checkpoint=checkpoint,
            stop=stop or threading.Event(),
        )
        self.assertIsNone(transport.process)
        return result

    def test_real_fake_process_round_trip(self):
        value = self.exchange(
            "import json,sys; value=json.load(sys.stdin); print(json.dumps({'received':bool(value['token'])}))"
        )
        self.assertEqual(value, {"received": True})

    def test_closed_gate_prevents_process_start(self):
        with patch("coding_runner.codex_backend.subprocess.Popen") as start:
            with self.assertRaises(BackendInterrupted):
                self.exchange("raise SystemExit(0)", checkpoint=lambda: False)
        start.assert_not_called()

    def test_shutdown_during_credential_lookup_prevents_process_start(self):
        stop = threading.Event()

        def credentials(account):
            stop.set()
            return grant(account)

        backend = CodexBackend(
            credentials, transport=FakeProcess("raise SystemExit(0)")
        )
        with patch("coding_runner.codex_backend.subprocess.Popen") as start:
            with self.assertRaises(BackendInterrupted):
                backend.research(
                    BackendRequest(ACCOUNT, "model", "", {}),
                    progress=Mock(),
                    checkpoint=lambda: True,
                    stop=stop,
                )
        start.assert_not_called()

    def test_full_backend_with_real_fake_process_validates_proposal(self):
        script = (
            "import json,sys; request=json.load(sys.stdin); print("
            + repr(json.dumps(response()))
            + ")"
        )
        backend = CodexBackend(grant, transport=FakeProcess(script))
        event = backend.research(
            BackendRequest(ACCOUNT, "model", "", {}),
            progress=Mock(),
            checkpoint=lambda: True,
            stop=threading.Event(),
        )
        self.assertEqual(event, BackendEvent("read", {"path": "app.py"}))

    def test_full_backend_fake_process_authentication_failure(self):
        backend = CodexBackend(
            grant, transport=FakeProcess('print(\'{"error":"authentication"}\')')
        )
        with self.assertRaises(BackendAuthenticationError):
            backend.research(
                BackendRequest(ACCOUNT, "model", "", {}),
                progress=Mock(),
                checkpoint=lambda: True,
                stop=threading.Event(),
            )

    def test_fake_process_has_no_credential_environment_or_arguments(self):
        script = "import os,sys,json; value=json.load(sys.stdin); print(json.dumps({'environment':dict(os.environ),'args':sys.argv}))"
        value = self.exchange(script)
        self.assertNotIn(TOKEN, json.dumps(value))

    def test_fake_process_malformed_json_fails(self):
        with self.assertRaises(BackendOutputError):
            self.exchange("print('not json')")

    def test_fake_process_exit_failure_fails(self):
        with self.assertRaises(BackendOutputError):
            self.exchange("raise SystemExit(2)")

    def test_fake_process_oversized_output_fails(self):
        with self.assertRaises(BackendOutputError):
            self.exchange("print('x' * (4 * 1024 * 1024 + 1))")

    def test_fake_process_timeout_stops_process(self):
        transport = FakeProcess("import time; time.sleep(10)")
        with self.assertRaises(BackendTimeout):
            transport.exchange(
                {}, timeout=0.05, checkpoint=lambda: True, stop=threading.Event()
            )
        self.assertIsNone(transport.process)

    def test_fake_process_periodic_pause_stops_process(self):
        calls = 0

        def checkpoint():
            nonlocal calls
            calls += 1
            return calls < 3

        with self.assertRaises(BackendInterrupted):
            self.exchange("import time; time.sleep(10)", checkpoint=checkpoint)

    def test_fake_process_shutdown_stops_process(self):
        stop = threading.Event()
        timer = threading.Timer(0.05, stop.set)
        timer.start()
        try:
            with self.assertRaises(BackendInterrupted):
                self.exchange("import time; time.sleep(10)", stop=stop)
        finally:
            timer.join()

    def test_explicit_interrupt_stops_active_process(self):
        transport = FakeProcess("import time; time.sleep(10)")
        timer = threading.Timer(0.05, transport.interrupt)
        timer.start()
        try:
            with self.assertRaises(BackendInterrupted):
                transport.exchange(
                    {}, timeout=1, checkpoint=lambda: True, stop=threading.Event()
                )
        finally:
            timer.join()
        self.assertIsNone(transport.process)

    def assert_exit_before_signal_is_reaped(self, target_signal):
        duration = 0.3 if target_signal == signal.SIGKILL else 0.02
        transport = FakeProcess("import time; time.sleep(" + str(duration) + ")")
        backend = CodexBackend(grant, transport=transport)
        killpg = os.killpg
        exited = []

        def exit_before_signal(pid, signum):
            if signum != target_signal:
                return
            process = transport.process
            self.assertIsNotNone(process)
            process.wait = Mock(wraps=process.wait)
            process.wait(timeout=1)
            exited.append(process)
            killpg(pid, signum)

        with patch(
            "coding_runner.codex_backend.os.killpg", side_effect=exit_before_signal
        ):
            with self.assertRaises(BackendInterrupted):
                backend.research(
                    BackendRequest(ACCOUNT, "model", "", {}),
                    progress=Mock(),
                    checkpoint=Mock(side_effect=[True, True, False]),
                    stop=threading.Event(),
                )
        self.assertEqual(len(exited), 1)
        self.assertEqual(exited[0].returncode, 0)
        self.assertGreaterEqual(exited[0].wait.call_count, 2)
        self.assertIsNone(transport.process)

    def test_exit_before_cleanup_sigterm_preserves_interruption_and_reaps(self):
        self.assert_exit_before_signal_is_reaped(signal.SIGTERM)

    def test_exit_before_cleanup_sigkill_preserves_interruption_and_reaps(self):
        self.assert_exit_before_signal_is_reaped(signal.SIGKILL)


class WorkerTests(unittest.TestCase):
    def exchange(self, *, status=200, body=None):
        payload = {
            "token": TOKEN,
            "timeout": 3,
            "request": {"model": "model", "tools": [], "tool_choice": "none"},
        }
        incoming = SimpleNamespace(buffer=io.BytesIO(json.dumps(payload).encode()))
        outgoing = io.StringIO()
        connection = Mock()
        remote = connection.getresponse.return_value
        remote.status = status
        remote.read.return_value = (
            body if body is not None else b'{"status":"completed"}'
        )
        with (
            patch("coding_runner.model_worker.sys.stdin", incoming),
            patch(
                "coding_runner.model_worker.http.client.HTTPSConnection",
                return_value=connection,
            ) as factory,
            redirect_stdout(outgoing),
        ):
            worker_main()
        self.assertEqual(factory.call_args.args, ("api.openai.com",))
        self.assertEqual(factory.call_args.kwargs, {"timeout": 3})
        self.assertEqual(connection.request.call_count, 1)
        return json.loads(outgoing.getvalue()), connection

    def test_worker_uses_fixed_endpoint_and_authorization_header(self):
        value, connection = self.exchange()
        method, path, body, headers = connection.request.call_args.args
        self.assertEqual((method, path), ("POST", "/v1/responses"))
        self.assertEqual(headers["Authorization"], "Bearer " + TOKEN)
        self.assertNotIn(TOKEN, body)
        self.assertEqual(value, {"response": {"status": "completed"}})

    def test_worker_withholds_authentication_body(self):
        value, connection = self.exchange(status=401, body=TOKEN.encode())
        self.assertEqual(value, {"error": "authentication"})
        connection.getresponse.return_value.read.assert_not_called()

    def test_worker_does_not_follow_redirects_or_retry(self):
        value, connection = self.exchange(status=307, body=TOKEN.encode())
        self.assertEqual(value, {"error": "model"})
        connection.getresponse.return_value.read.assert_not_called()

    def test_worker_withholds_malformed_json_body(self):
        value, connection = self.exchange(body=TOKEN.encode())
        self.assertEqual(value, {"error": "transport"})

    def test_worker_rejects_oversized_response(self):
        value, connection = self.exchange(body=b"x" * (4 * 1024 * 1024 + 1))
        self.assertEqual(value, {"error": "transport"})


class RegistryTests(unittest.TestCase):
    def test_preflight_secret_scope_only_contains_explicit_account(self):
        client = Mock()
        ScopedCredentials(client)({**ACCOUNT, "project_ids": [1]})
        self.assertEqual(
            client.request.call_args.args[2], {"scope": "preflight", **ACCOUNT}
        )

    def test_run_secret_scope_cannot_choose_arbitrary_account(self):
        client = Mock()
        selected = ScopedCredentials(
            client, run_id=str(uuid4()), claim_token=str(uuid4()), stage="research"
        )
        selected(ACCOUNT)
        payload = client.request.call_args.args[2]
        self.assertEqual(set(payload), {"scope", "run_id", "claim_token", "purpose"})
        self.assertEqual(payload["purpose"], "research")

    def test_usage_adapters_keep_distinct_codex_accounts(self):
        accounts = [ACCOUNT, {"backend": "codex", "credential_id": 8}]
        adapters = BackendRegistry().usage_adapters(accounts, ScopedCredentials(Mock()))
        self.assertEqual(set(adapters), {("codex", 7), ("codex", 8)})
        self.assertIsNot(adapters[("codex", 7)], adapters[("codex", 8)])

    def test_unsupported_allowance_blocks_all_configured_accounts(self):
        with (
            tempfile.TemporaryDirectory() as temporary,
            Journal(Path(temporary) / "journal") as journal,
        ):
            config = copy.deepcopy(CONFIG)
            config["global"]["reserve_enabled"] = True
            config["accounts"] = [ACCOUNT, {"backend": "codex", "credential_id": 8}]
            adapters = BackendRegistry().usage_adapters(
                config["accounts"], ScopedCredentials(Mock())
            )
            gates = GateEvaluator(journal, clock=lambda: NOW, adapters=adapters)
            decision = gates.evaluate(config, journal.load())
        self.assertFalse(decision.allowed)
        self.assertEqual(len(decision.readings), 2)
        self.assertEqual({row["credential_id"] for row in decision.readings}, {7, 8})


class ResearchTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.journal = Journal(root / "journal")
        self.journal.__enter__()
        self.addCleanup(self.journal.__exit__)
        self.state = self.journal.load()
        self.run_id = str(uuid4())
        checkout = root / "checkout"
        checkout.mkdir()
        (checkout / "app.py").write_text("value = 1\n")
        self.workspace = SimpleNamespace(
            workspace_id=self.run_id,
            base_revision=BASE,
            resolved_base_branch="main",
            checkout=checkout,
            locked=nullcontext,
            verify=lambda: content_digest(checkout),
        )
        self.item = {
            "source": "manual",
            "title": "Change value",
            "instructions": "Inspect and plan",
            "repository_url": "https://github.com/example/repo",
            "base_branch": "main",
            "branch_name": "task",
        }
        document = manual_context(self.item)
        self.context = {
            "id": str(uuid4()),
            "document": document,
            "digest": digest(document),
            "inspected_base_commit": BASE,
            "resolved_base_branch": "main",
        }
        self.sources = SourceStore(root / "sources")
        source_digest = self.sources.register(
            "operator", "manual-task", b"operator instructions"
        )
        self.context["sources"] = [
            {"kind": "operator", "reference": "manual-task", "digest": source_digest}
        ]
        self.backend = Mock()
        self.backend.readiness.return_value = BackendReadiness(
            True, True, (), CAPABILITIES
        )
        self.backend.research.return_value = BackendEvent("read", {"path": "app.py"})
        self.executor = Mock(read_only=True)
        self.executor.broker.request.return_value = {
            "id": str(uuid4()),
            "decision": "pending",
        }
        self.store = Mock()
        self.store.save_plan.return_value = {"id": str(uuid4()), "version": 1}
        self.request = BackendRequest(ACCOUNT, "model", "instructions", {})
        self.stage = ResearchStage(
            self.backend,
            self.journal,
            self.store,
            self.executor,
            self.sources,
            checkpoint=lambda: True,
            progress=Mock(),
        )

    def step(self):
        return self.stage.step(
            self.state,
            self.run_id,
            self.item,
            self.context,
            self.workspace,
            self.request,
        )

    def action(self):
        self.step()
        observation = self.state["research"][self.run_id]["observations"][0]
        action = ActionSpec(
            ("/usr/bin/true",),
            "exec",
            "repository",
            "app.py",
            observation["digest"],
            self.run_id,
            BASE,
            self.workspace.verify(),
        )
        self.backend.research.return_value = BackendEvent(
            "action", {"action": action.to_data()}
        )
        return action

    def plan(self):
        self.step()
        observation = self.state["research"][self.run_id]["observations"][0]
        source = {key: observation[key] for key in ("kind", "reference", "digest")}
        action = ActionSpec(
            ("/usr/bin/true",),
            "exec",
            "repository",
            "app.py",
            observation["digest"],
            self.run_id,
            BASE,
            self.workspace.verify(),
        ).to_data()
        return {
            "schema_version": 1,
            "context_id": self.context["id"],
            "context_digest": self.context["digest"],
            "problem": "Value differs",
            "expected_behavior": "Value matches",
            "acceptance_criteria": ["Value matches"],
            "findings": ["Inspected app.py"],
            "sources": [source],
            "relevant_paths": ["app.py"],
            "approach": "Update the value",
            "tradeoffs": [],
            "steps": ["Edit value"],
            "file_changes": ["app.py"],
            "setup_actions": [],
            "validation_actions": [action],
            "regression_cases": [],
            "delivery_evidence": ["Validation result"],
            "blockers": [],
            "open_questions": [],
            "original_issue": None,
            "checkout": self.context["document"]["checkout"],
            "inspected_base_commit": BASE,
            "resolved_base_branch": "main",
        }

    def test_file_reads_are_data_and_feed_next_model_turn(self):
        self.assertEqual(self.step().status, "continue")
        self.step()
        document = self.backend.research.call_args.args[0].document
        self.assertEqual(document["observations"][0]["content"], "value = 1\n")
        self.executor.execute.assert_not_called()

    def test_required_approval_survives_restart_without_model_retry(self):
        self.action()
        result = self.step()
        self.assertEqual(result.status, "approval")
        self.state = self.journal.load()
        self.step()
        self.assertEqual(self.backend.research.call_count, 2)
        self.assertEqual(
            self.executor.broker.request.call_args_list[-1].args[1],
            self.executor.broker.request.call_args_list[-2].args[1],
        )
        self.executor.execute.assert_not_called()

    def test_only_human_approved_action_reaches_executor(self):
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

    def test_source_change_invalidates_proposal_before_approval_request(self):
        self.action()
        (self.workspace.checkout / "app.py").write_text("changed")
        with self.assertRaises(ApprovalRequired):
            self.step()
        self.executor.broker.request.assert_not_called()

    def test_unresolved_model_call_is_not_automatically_repeated(self):
        self.backend.research.side_effect = BackendTimeout("unknown")
        with self.assertRaises(BackendTimeout):
            self.step()
        self.state = self.journal.load()
        with self.assertRaisesRegex(BackendError, "unresolved"):
            self.step()
        self.assertEqual(self.backend.research.call_count, 1)

    def test_uncertain_approved_execution_is_not_repeated(self):
        self.action()
        self.executor.broker.request.return_value["decision"] = "approved"
        self.executor.execute.side_effect = OSError("supervisor interrupted")
        with self.assertRaises(OSError):
            self.step()
        self.state = self.journal.load()
        with self.assertRaisesRegex(BackendError, "operator recovery"):
            self.step()
        self.assertEqual(self.executor.execute.call_count, 1)

    def test_structured_plan_is_validated_and_saved_once(self):
        plan = self.plan()
        self.backend.research.return_value = BackendEvent("plan", {"document": plan})
        self.assertEqual(self.step().status, "saved")
        self.assertEqual(self.step().status, "saved")
        self.assertEqual(self.store.save_plan.call_count, 1)

    def test_malformed_plan_cannot_be_saved(self):
        plan = self.plan()
        del plan["acceptance_criteria"]
        self.backend.research.return_value = BackendEvent("plan", {"document": plan})
        with self.assertRaises(ValueError):
            self.step()
        self.store.save_plan.assert_not_called()

    def test_plan_cannot_bind_validation_to_another_workspace(self):
        plan = self.plan()
        plan["validation_actions"][0]["workspace_id"] = str(uuid4())
        self.backend.research.return_value = BackendEvent("plan", {"document": plan})
        with self.assertRaisesRegex(BackendOutputError, "current inspected workspace"):
            self.step()
        self.store.save_plan.assert_not_called()

    def test_plan_cannot_bind_setup_to_old_workspace_content(self):
        plan = self.plan()
        action = copy.deepcopy(plan["validation_actions"][0])
        action["content_digest"] = "0" * 64
        plan["setup_actions"] = [action]
        self.backend.research.return_value = BackendEvent("plan", {"document": plan})
        with self.assertRaisesRegex(BackendOutputError, "current inspected workspace"):
            self.step()
        self.store.save_plan.assert_not_called()

    def test_uninspected_plan_is_rejected(self):
        self.backend.research.return_value = BackendEvent("plan", {"document": {}})
        with self.assertRaisesRegex(BackendOutputError, "Inspect checkout"):
            self.step()

    def test_research_never_invokes_implementation_or_publishing(self):
        for kind in ("patch", "implementation", "publish", "complete"):
            self.state = self.journal.load()
            self.state.pop("research", None)
            self.backend.research.return_value = BackendEvent(kind, {})
            with self.subTest(kind=kind), self.assertRaises(BackendOutputError):
                self.step()
        self.backend.implementation.assert_not_called()
        self.store.save_plan.assert_not_called()

    def test_writable_executor_cannot_be_used_for_research(self):
        self.executor.read_only = False
        with self.assertRaises(BackendError):
            ResearchStage(
                self.backend,
                self.journal,
                self.store,
                self.executor,
                self.sources,
                checkpoint=lambda: True,
                progress=Mock(),
            )

    def test_source_manifest_tampering_is_rejected(self):
        self.context["sources"][0]["digest"] = "0" * 64
        with self.assertRaises(ApprovalRequired):
            self.step()
        self.backend.research.assert_not_called()

    def test_plan_cannot_cite_unread_repository_content(self):
        plan = self.plan()
        plan["sources"] = [self.context["sources"][0]]
        plan["validation_actions"][0].update(
            source_kind="operator",
            source_reference="manual-task",
            source_digest=self.context["sources"][0]["digest"],
        )
        self.backend.research.return_value = BackendEvent("plan", {"document": plan})
        with self.assertRaisesRegex(BackendOutputError, "must cite"):
            self.step()
        self.store.save_plan.assert_not_called()

    def test_closed_gate_prevents_model_and_command(self):
        self.stage.checkpoint = lambda: False
        with self.assertRaises(BackendInterrupted):
            self.step()
        self.backend.research.assert_not_called()
        self.executor.execute.assert_not_called()

    def test_total_active_budget_bounds_model_and_command_timeouts(self):
        budget = Mock()
        budget.checkpoint.return_value = True
        budget.run.side_effect = lambda stage, operation: operation(3)
        self.stage.budget = budget
        self.action()
        self.assertEqual(self.backend.research.call_args.args[0].timeout_seconds, 3)
        self.executor.broker.request.return_value["decision"] = "approved"
        self.executor.execute.return_value = SimpleNamespace(
            execution_id=str(uuid4()), exit_code=0, output=b"result", interrupted=False
        )
        self.step()
        self.assertEqual(self.executor.execute.call_args.kwargs["timeout"], 3)

    def test_closed_budget_prevents_model_work(self):
        budget = Mock()
        budget.checkpoint.return_value = False
        self.stage.budget = budget
        with self.assertRaises(BackendInterrupted):
            self.step()
        self.backend.research.assert_not_called()

    def test_unverified_authentication_prevents_research(self):
        self.backend.readiness.return_value = BackendReadiness(
            False, False, ("unverified",), CAPABILITIES
        )
        with self.assertRaises(BackendError):
            self.step()
        self.backend.research.assert_not_called()

    def test_read_does_not_follow_directory_symlinks(self):
        (self.workspace.checkout / "link").symlink_to(
            self.workspace.checkout, target_is_directory=True
        )
        with self.assertRaises(BackendOutputError):
            read_checkout(self.workspace, "link/app.py")

    def test_read_does_not_expose_git_or_parent(self):
        for path in (".git/config", "../secret", "/etc/passwd"):
            with self.subTest(path=path), self.assertRaises(BackendOutputError):
                read_checkout(self.workspace, path)

    def test_read_rejects_special_files_without_blocking(self):
        os.mkfifo(self.workspace.checkout / "fifo")
        with self.assertRaises(BackendOutputError):
            read_checkout(self.workspace, "fifo")


if __name__ == "__main__":
    unittest.main()
