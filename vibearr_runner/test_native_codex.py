import copy
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import json
import unittest
from uuid import uuid4

from vibearr_runner.native_codex import CodexEvents
from vibearr_runner.native_cli import NativeCLIAdapter
from vibearr_runner.native_sessions import NativeSession, NativeSessionError
from vibearr_runner.policy import default_execution_policy

EXECUTOR_INSTALLED = importlib.util.find_spec("vibearr_executor") is not None
if EXECUTOR_INSTALLED:
    from vibearr_executor import SLOT_QUALIFICATION_VERSION as QUALIFICATION_VERSION
    from vibearr_executor.artifacts import pack, result_manifest
    from vibearr_executor.contracts import Conflict
    from vibearr_executor.compact_profiles import profiles


LOGIN_DIGEST = "d" * 64


def credential():
    return {
        "credential": {"id": 1, "provider": "codex", "kind": "login", "version": 2},
    }


class ExecutorFixture:
    def __init__(self):
        installed, _ = profiles("sha256:" + "a" * 64, "b" * 64)
        self.profile = next(
            value for value in installed if value.profile_id == "native-default"
        )
        self.compact = True
        self.identity = {
            "runtime": "compose-slot-v2",
            "protocol_version": 2,
            "binding_digest": "b" * 64,
            "instance_id": str(uuid4()),
            "worker_boot": str(uuid4()),
        }
        self.prepared = {}
        self.starts = 0
        self.secrets = {}
        self.content = b""
        self.state = "not-started"
        self.receipt = None
        self.qualified = True
        self.accepting_work = True
        self.qualification_version = QUALIFICATION_VERSION
        self.binding_digest = "b" * 64
        self.export_unchanged = False

    def capabilities(self):
        return {
            "runtime": "compact-worker-v1",
            "schema_version": 1,
            "protocol_version": 2,
            "qualification_version": self.qualification_version,
            "binding_digest": self.binding_digest,
            "qualified": self.qualified,
            "accepting_work": self.accepting_work,
            "profiles": [
                {**self.profile.data(), "profile_digest": self.profile.digest}
            ],
        }

    def upload(self, kind, content):
        return {"sha256": hashlib.sha256(content).hexdigest()}

    def prepare(self, manifest):
        operation = manifest["operation_id"]
        if operation in self.prepared and self.prepared[operation] != manifest:
            raise Conflict("Manifest changed")
        self.prepared[operation] = copy.deepcopy(manifest)
        return {
            "operation_id": operation,
            "state": self.state,
            "execution_identity": self.identity,
        }

    def request(self, path):
        return {"available": True, "login_digest": LOGIN_DIGEST}

    def supply_secret(self, operation, secret):
        self.secrets[operation] = secret

    def start(self, operation):
        if self.state == "not-started":
            self.starts += 1
            self.state = "running"
        return self.status(operation)

    def status(self, operation):
        result = {
            "operation_id": operation,
            "state": self.state,
            "container_id": "container-original",
        }
        if self.state == "exited":
            result.update(descendants_terminated=True, result=self.receipt)
        return result

    def logs(self, operation, offset=0, limit=65536):
        return self.content[offset : offset + limit]

    def stop(self, operation):
        self.complete(operation, exit_code=-9)
        self.receipt["canceled"] = True
        return self.status(operation)

    def complete(self, operation, *, exit_code=0, truncated=False):
        self.state = "exited"
        self.receipt = {
            "run_id": self.prepared[operation]["run_id"],
            "operation_id": operation,
            "exit_code": exit_code,
            "timed_out": False,
            "canceled": False,
            "log_truncated": truncated,
            "log_digest": hashlib.sha256(self.content).hexdigest(),
            "ended_at": datetime.now(timezone.utc).timestamp(),
        }

    def export(self, operation, source):
        if self.export_unchanged:
            return result_manifest(source, source), source
        exported = pack({"answer.py": (0o644, b"answer = 42\n")})
        return result_manifest(source, exported), exported


@unittest.skipUnless(
    EXECUTOR_INSTALLED,
    "Install the native-runtime extra for executor-backed native session tests.",
)
class NativeCodexTests(unittest.TestCase):
    def setUp(self):
        self.client = ExecutorFixture()
        self.adapter = NativeCLIAdapter(self.client, "codex")
        self.policy = default_execution_policy()
        self.policy["runtime_profile"] = self.client.profile.profile_id
        self.source = pack({"answer.py": (0o644, b"answer = 0\n")})
        self.request = {
            "run_id": str(uuid4()),
            "operation_id": str(uuid4()),
            "source": self.source,
            "prompt": "Set answer to 42 and run its tests.",
            "model": "gpt-6.1-sol",
            "policy": self.policy,
            "deadline": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
            "credential": credential(),
        }

    def prepare(self):
        return self.adapter.prepare(**self.request)

    def completed_events(self):
        return (
            json.dumps({"type": "thread.started", "thread_id": str(uuid4())})
            + "\n"
            + json.dumps(
                {
                    "type": "turn.completed",
                    "usage": {"input_tokens": 12, "output_tokens": 4},
                }
            )
            + "\n"
        ).encode()

    def test_prepare_preserves_selected_account_version_and_exact_manifest(self):
        session = self.prepare()
        self.assertEqual(session.account["credential_version"], 2)
        self.assertEqual(
            self.client.prepared[session.manifest["operation_id"]], session.manifest
        )
        self.assertEqual(
            json.loads(self.client.secrets[session.manifest["operation_id"]]),
            {"provider": "codex", "credential_id": 1, "login_digest": LOGIN_DIGEST},
        )

    def test_retired_token_session_record_is_rejected(self):
        value = self.prepare().to_data()
        value["auth_kind"] = "token"
        with self.assertRaises(NativeSessionError):
            NativeSession.from_data(value)

    def test_retired_raw_subscription_session_record_is_rejected(self):
        value = self.prepare().to_data()
        value["auth_kind"] = "session"
        with self.assertRaises(NativeSessionError):
            NativeSession.from_data(value)

    def test_smaller_policy_limits_require_a_matching_fixed_worker_slot(self):
        self.policy.update(cpu_limit=1, memory_mb=1024, pids_limit=32, workspace_mb=256)
        with self.assertRaises(NativeSessionError):
            self.prepare()
        self.assertFalse(self.client.prepared)

    def test_unqualified_runtime_cannot_prepare_native_work(self):
        self.client.qualified = False
        with self.assertRaises(NativeSessionError):
            self.prepare()
        self.assertFalse(self.client.prepared)

    def test_stale_executor_qualification_contract_is_rejected(self):
        self.client.qualification_version = QUALIFICATION_VERSION - 1
        with self.assertRaises(NativeSessionError):
            self.prepare()
        self.assertFalse(self.client.prepared)

    def test_missing_auth_fails_before_artifact_or_operation_creation(self):
        self.request["credential"] = {}
        with self.assertRaises(NativeSessionError):
            self.prepare()
        self.assertFalse(self.client.prepared)

    def test_changed_recipe_digest_is_rejected(self):
        original = self.client.capabilities
        self.client.capabilities = lambda: {
            **original(),
            "profiles": [{**original()["profiles"][0], "profile_digest": "f" * 64}],
        }
        with self.assertRaises(NativeSessionError):
            self.prepare()

    def test_forbidden_model_arguments_are_rejected(self):
        self.request["model"] = "--model=https://example.com"
        with self.assertRaises(ValueError):
            self.prepare()

    def test_model_egress_cannot_expand_policy(self):
        self.policy["network_hosts"] = ["pypi.org"]
        with self.assertRaises(NativeSessionError):
            self.prepare()

    def test_repeated_start_reuses_same_container(self):
        session = self.prepare()
        first = self.adapter.start(session)
        second = self.adapter.start(session)
        self.assertEqual(first["container_id"], second["container_id"])
        self.assertEqual(self.client.starts, 1)

    def test_retry_of_running_work_is_allowed_when_the_fleet_has_no_new_capacity(self):
        session = self.prepare()
        self.adapter.start(session)
        self.client.accepting_work = False
        self.assertEqual(self.adapter.start(session)["state"], "running")
        self.assertEqual(self.client.starts, 1)

    def test_busy_fleet_cannot_start_an_unstarted_operation(self):
        session = self.prepare()
        self.client.accepting_work = False
        with self.assertRaisesRegex(NativeSessionError, "capacity"):
            self.adapter.start(session)
        self.assertEqual(self.client.starts, 0)

    def test_recovery_round_trip_reconciles_same_operation(self):
        session = self.prepare()
        self.adapter.start(session)
        restored = NativeSession.from_data(session.to_data())
        self.assertEqual(self.adapter.reconcile(restored)["state"], "running")
        self.assertEqual(self.client.starts, 1)

    def test_unknown_outcome_remains_fenced_without_new_start(self):
        session = self.prepare()
        self.client.state = "unknown"
        self.assertEqual(self.adapter.reconcile(session)["state"], "unknown")
        with self.assertRaises(NativeSessionError):
            self.adapter.outcome(session, self.source)
        self.assertEqual(self.client.starts, 0)

    def test_modified_session_manifest_conflicts_on_reconcile(self):
        session = self.prepare()
        restored = session.to_data()
        restored["manifest"]["context_digest"] = "f" * 64
        with self.assertRaises(Conflict):
            self.adapter.reconcile(NativeSession.from_data(restored))

    def test_cancellation_does_not_launch_another_agent(self):
        session = self.prepare()
        self.adapter.start(session)
        self.assertTrue(self.adapter.cancel(session)["descendants_terminated"])
        self.adapter.start(session)
        self.assertEqual(self.client.starts, 1)

    def test_complete_session_verifies_events_logs_and_export(self):
        session = self.prepare()
        self.client.content = self.completed_events()
        self.client.complete(session.manifest["operation_id"])
        result = self.adapter.outcome(session, self.source)
        self.assertTrue(result["successful"])
        self.assertEqual(result["usage"], {"input_tokens": 12, "output_tokens": 4})
        self.assertFalse(result["quota_supported"])

    def test_agent_summary_without_turn_completion_is_not_success(self):
        session = self.prepare()
        self.client.content = b'{"type":"item.completed","item":{"type":"agent_message","text":"All done"}}\n'
        self.client.complete(session.manifest["operation_id"])
        self.assertFalse(self.adapter.outcome(session, self.source)["successful"])

    def test_truncated_log_cannot_prove_success(self):
        session = self.prepare()
        self.client.content = self.completed_events()
        self.client.complete(session.manifest["operation_id"], truncated=True)
        self.assertFalse(self.adapter.outcome(session, self.source)["successful"])

    def test_wrong_run_receipt_is_rejected(self):
        session = self.prepare()
        self.client.complete(session.manifest["operation_id"])
        self.client.receipt["run_id"] = str(uuid4())
        with self.assertRaises(NativeSessionError):
            self.adapter.inspect(session)

    def test_tampered_log_is_rejected(self):
        session = self.prepare()
        self.client.content = self.completed_events()
        self.client.complete(session.manifest["operation_id"])
        self.client.content += b"tampered\n"
        with self.assertRaises(NativeSessionError):
            self.adapter.outcome(session, self.source)

    def test_diagnostics_do_not_claim_live_auth_from_selected_login(self):
        result = self.adapter.diagnostics(
            self.policy, credential(), model=self.request["model"]
        )
        self.assertTrue(result["auth_available"])
        self.assertFalse(result["authenticated"])
        self.assertFalse(result["native_agent_ready"])

    def test_authentication_probe_produces_bound_live_proof(self):
        request = {
            key: value
            for key, value in self.request.items()
            if key not in {"source", "prompt"}
        }
        session = self.adapter.prepare_auth_probe(**request)
        self.client.content = self.completed_events()
        self.client.export_unchanged = True
        self.client.complete(session.manifest["operation_id"])
        proof = self.adapter.authentication_result(session)
        result = self.adapter.diagnostics(
            self.policy, credential(), model=self.request["model"], proof=proof
        )
        self.assertTrue(result["authenticated"])
        self.assertTrue(result["native_agent_ready"])

    def test_regular_task_cannot_be_used_as_fixed_authentication_probe(self):
        session = self.prepare()
        with self.assertRaises(NativeSessionError):
            self.adapter.authentication_result(session)

    def test_authentication_probe_cannot_be_relabeled_as_another_credential(self):
        request = {
            key: value
            for key, value in self.request.items()
            if key not in {"source", "prompt"}
        }
        session = self.adapter.prepare_auth_probe(**request)
        changed = session.to_data()
        changed["account"]["credential_id"] += 1
        with self.assertRaises(NativeSessionError):
            self.adapter.authentication_result(NativeSession.from_data(changed))

    def test_replaying_probe_preserves_original_authentication_expiry(self):
        request = {
            key: value
            for key, value in self.request.items()
            if key not in {"source", "prompt"}
        }
        session = self.adapter.prepare_auth_probe(**request)
        self.client.content = self.completed_events()
        self.client.export_unchanged = True
        self.client.complete(session.manifest["operation_id"])
        first = self.adapter.authentication_result(session)
        second = self.adapter.authentication_result(session)
        self.assertEqual(first["verified_at"], second["verified_at"])
        self.assertEqual(first["expires_at"], second["expires_at"])

    def test_old_completed_probe_cannot_mint_new_authentication_proof(self):
        request = {
            key: value
            for key, value in self.request.items()
            if key not in {"source", "prompt"}
        }
        session = self.adapter.prepare_auth_probe(**request)
        self.client.content = self.completed_events()
        self.client.export_unchanged = True
        self.client.complete(session.manifest["operation_id"])
        self.client.receipt["ended_at"] = (
            datetime.now(timezone.utc) - timedelta(minutes=16)
        ).timestamp()
        with self.assertRaises(NativeSessionError):
            self.adapter.authentication_result(session)

    def test_future_completed_probe_cannot_mint_authentication_proof(self):
        request = {
            key: value
            for key, value in self.request.items()
            if key not in {"source", "prompt"}
        }
        session = self.adapter.prepare_auth_probe(**request)
        self.client.content = self.completed_events()
        self.client.export_unchanged = True
        self.client.complete(session.manifest["operation_id"])
        self.client.receipt["ended_at"] = (
            datetime.now(timezone.utc) + timedelta(minutes=1)
        ).timestamp()
        with self.assertRaises(NativeSessionError):
            self.adapter.authentication_result(session)

    def test_credential_rotation_invalidates_authentication_proof(self):
        request = {
            key: value
            for key, value in self.request.items()
            if key not in {"source", "prompt"}
        }
        session = self.adapter.prepare_auth_probe(**request)
        self.client.content = self.completed_events()
        self.client.export_unchanged = True
        self.client.complete(session.manifest["operation_id"])
        proof = self.adapter.authentication_result(session)
        selected = credential()
        selected["credential"]["version"] += 1
        self.assertFalse(
            self.adapter.diagnostics(
                self.policy, selected, model=self.request["model"], proof=proof
            )["authenticated"]
        )

    def test_changed_runtime_blocks_start_but_allows_same_operation_reconciliation(
        self,
    ):
        session = self.prepare()
        self.adapter.start(session)
        self.client.binding_digest = "c" * 64
        with self.assertRaises(NativeSessionError):
            self.adapter.start(session)
        self.assertEqual(self.adapter.reconcile(session)["state"], "running")
        self.assertEqual(self.client.starts, 1)

    def test_malformed_authentication_proof_is_an_unready_diagnostic(self):
        result = self.adapter.diagnostics(
            self.policy, credential(), model=self.request["model"], proof=[]
        )
        self.assertFalse(result["authenticated"])

    def test_expired_authentication_proof_is_not_ready(self):
        request = {
            key: value
            for key, value in self.request.items()
            if key not in {"source", "prompt"}
        }
        session = self.adapter.prepare_auth_probe(**request)
        self.client.content = self.completed_events()
        self.client.export_unchanged = True
        self.client.complete(session.manifest["operation_id"])
        proof = self.adapter.authentication_result(session)
        proof["verified_at"] = (
            datetime.now(timezone.utc) - timedelta(hours=2)
        ).isoformat()
        proof["expires_at"] = (
            datetime.now(timezone.utc) - timedelta(hours=1)
        ).isoformat()
        self.assertFalse(
            self.adapter.diagnostics(
                self.policy, credential(), model=self.request["model"], proof=proof
            )["authenticated"]
        )


class CodexEventTests(unittest.TestCase):
    def test_split_json_event_is_reassembled(self):
        parser = CodexEvents()
        parser.feed(b'{"type":"turn.')
        self.assertFalse(parser.events)
        parser.feed(b'started"}\n')
        self.assertEqual(parser.events, [{"kind": "turn.started"}])

    def test_oversized_output_is_rejected(self):
        with self.assertRaises(NativeSessionError):
            CodexEvents().feed(b"a" * (1024 * 1024 + 1))

    def large_command_event(self):
        return (
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "type": "command_execution",
                        "command": "python manage.py test",
                        "aggregated_output": "ordinary test diagnostic\n" * 12000,
                        "status": "failed",
                        "exit_code": 1,
                    },
                }
            ).encode()
            + b"\n"
        )

    def test_large_failed_command_output_is_summarized_before_turn_completion(self):
        parser = CodexEvents()
        event = self.large_command_event()
        self.assertGreater(len(event), 256 * 1024)
        parser.feed(event + b'{"type":"turn.completed","usage":{}}\n')
        self.assertTrue(parser.completed)
        self.assertFalse(parser.failed)
        self.assertEqual(parser.events[0]["command"], "python manage.py test")
        self.assertNotIn("ordinary test diagnostic", json.dumps(parser.events))

    def test_large_frame_keeps_command_and_message_summaries_bounded(self):
        command = json.loads(self.large_command_event())
        command["item"]["command"] = "x" * 5000
        message = {
            "type": "item.completed",
            "item": {"type": "agent_message", "text": "y" * 5000},
        }
        parser = CodexEvents()
        parser.feed((json.dumps(command) + "\n" + json.dumps(message) + "\n").encode())
        self.assertEqual(len(parser.events[0]["command"]), 4000)
        self.assertEqual(len(parser.events[1]["text"]), 4000)
        self.assertNotIn("aggregated_output", parser.events[0])

    def test_large_command_event_is_reassembled_across_bounded_chunks(self):
        parser = CodexEvents()
        event = self.large_command_event()
        for offset in range(0, len(event), 65536):
            parser.feed(event[offset : offset + 65536])
        self.assertEqual(len(parser.events), 1)
        self.assertFalse(parser.completed)
        self.assertEqual(parser.pending, b"")

    def test_oversized_event_has_a_distinct_byte_limit_error(self):
        with self.assertRaisesRegex(NativeSessionError, "event exceeds its byte limit"):
            CodexEvents().line(b"a" * (1024 * 1024 + 1))

    def test_whole_transcript_limit_survives_multiple_valid_large_events(self):
        parser = CodexEvents()
        event = self.large_command_event()
        parser.feed(event)
        parser.feed(event)
        parser.feed(event)
        with self.assertRaisesRegex(
            NativeSessionError, "output exceeds its byte limit"
        ):
            parser.feed(event)

    def test_event_count_limit_remains_separate_from_byte_budget(self):
        parser = CodexEvents()
        parser.feed(b'{"type":"turn.started"}\n' * 2048)
        with self.assertRaisesRegex(
            NativeSessionError, "event count exceeds its limit"
        ):
            parser.feed(b'{"type":"turn.started"}\n')

    def test_non_object_event_is_rejected_with_larger_frame_budget(self):
        with self.assertRaisesRegex(NativeSessionError, "event must be an object"):
            CodexEvents().feed(json.dumps(["x" * 280000]).encode() + b"\n")

    def test_large_command_does_not_invent_terminal_success(self):
        parser = CodexEvents()
        parser.feed(self.large_command_event())
        self.assertFalse(parser.completed)
        parser.feed(b'{"type":"turn.failed"}\n')
        self.assertTrue(parser.failed)
        self.assertFalse(parser.completed)

    def test_changed_session_identity_is_rejected(self):
        parser = CodexEvents()
        parser.feed(
            (
                json.dumps({"type": "thread.started", "thread_id": str(uuid4())}) + "\n"
            ).encode()
        )
        with self.assertRaises(NativeSessionError):
            parser.feed(
                (
                    json.dumps({"type": "thread.started", "thread_id": str(uuid4())})
                    + "\n"
                ).encode()
            )

    def test_failed_event_prevents_success(self):
        parser = CodexEvents()
        parser.feed(
            b'{"type":"turn.failed","error":{"message":"untrusted provider text"}}\n'
        )
        self.assertTrue(parser.failed)
        self.assertNotIn("untrusted provider text", json.dumps(parser.events))

    def test_recovered_transport_error_does_not_invalidate_completed_turn(self):
        parser = CodexEvents()
        parser.feed(
            b'{"type":"error","message":"Reconnecting"}\n{"type":"turn.completed","usage":{}}\n'
        )
        self.assertTrue(parser.completed)
        self.assertFalse(parser.failed)

    def test_usage_must_be_nonnegative_tokens(self):
        with self.assertRaises(NativeSessionError):
            CodexEvents().feed(
                b'{"type":"turn.completed","usage":{"input_tokens":-1}}\n'
            )
