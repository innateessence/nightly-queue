import copy
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import json
import unittest
from uuid import uuid4

from vibearr_runner.native_codex import CodexEvents, NativeCodexAdapter, codex_profile
from vibearr_runner.native_sessions import NativeSession, NativeSessionError
from vibearr_runner.policy import default_execution_policy

EXECUTOR_INSTALLED = importlib.util.find_spec("vibearr_executor") is not None
if EXECUTOR_INSTALLED:
    from vibearr_executor import QUALIFICATION_VERSION
    from vibearr_executor.artifacts import pack, result_manifest
    from vibearr_executor.contracts import Conflict


TOKEN = "test-codex-token-0123456789"


def credential():
    return {
        "credential": {"id": 1, "provider": "codex", "kind": "token", "version": 2},
        "material": {"secret": TOKEN},
    }


class ExecutorFixture:
    def __init__(self):
        self.profile = codex_profile("sha256:" + "a" * 64, "token")
        self.prepared = {}
        self.starts = 0
        self.secrets = {}
        self.content = b""
        self.state = "not-started"
        self.receipt = None
        self.qualified = True
        self.qualification_version = QUALIFICATION_VERSION
        self.binding_digest = "b" * 64
        self.export_unchanged = False

    def capabilities(self):
        return {
            "runtime": "docker-oci-v1",
            "schema_version": 1,
            "protocol_version": 1,
            "qualification_version": self.qualification_version,
            "binding_digest": self.binding_digest,
            "qualified": self.qualified,
            "accepting_work": True,
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
        return {"operation_id": operation, "state": self.state}

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
        self.adapter = NativeCodexAdapter(self.client)
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

    def test_review_purpose_requires_qualified_image_contract_before_preparation(self):
        with self.assertRaises(NativeSessionError):
            self.adapter.prepare(**self.request, purpose="review")
        self.assertEqual(self.client.prepared, {})
        self.assertEqual(self.client.starts, 0)

    def test_review_session_binds_fixed_purpose_in_trusted_context(self):
        original = self.client.capabilities

        def capabilities():
            value = original()
            value["profiles"][0]["native_review_purpose"] = True
            return value

        self.client.capabilities = capabilities
        uploaded = []
        upload = self.client.upload

        def capture(kind, content):
            uploaded.append((kind, content))
            return upload(kind, content)

        self.client.upload = capture
        session = self.adapter.prepare(**self.request, purpose="review")
        self.assertEqual(session.purpose, "review")
        self.assertEqual(session.to_data()["purpose"], "review")
        context = next(content for kind, content in uploaded if kind == "context")
        self.assertEqual(json.loads(context)["purpose"], "review")
        self.assertEqual(
            session.manifest["context_digest"], hashlib.sha256(context).hexdigest()
        )

    def test_old_coding_session_round_trip_preserves_immutable_shape(self):
        session = self.prepare()
        value = session.to_data()
        self.assertNotIn("purpose", value)
        self.assertEqual(NativeSession.from_data(value).to_data(), value)

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
        self.assertNotIn(TOKEN, json.dumps(session.to_data()))

    def test_manifest_uses_policy_limits_below_operator_ceiling(self):
        self.policy.update(cpu_limit=1, memory_mb=1024, pids_limit=32, workspace_mb=256)
        session = self.prepare()
        self.assertEqual(
            session.manifest["limits"],
            {"cpu": 1, "memory_mb": 1024, "pids": 32, "workspace_mb": 256},
        )

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
        self.client.profile = codex_profile(
            self.client.profile.image,
            "token",
            profile_id=self.client.profile.profile_id,
        )
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

    def test_selected_secret_in_prompt_is_rejected(self):
        self.request["prompt"] += TOKEN
        with self.assertRaises(NativeSessionError):
            self.prepare()

    def test_repeated_start_reuses_same_container(self):
        session = self.prepare()
        first = self.adapter.start(session)
        second = self.adapter.start(session)
        self.assertEqual(first["container_id"], second["container_id"])
        self.assertEqual(self.client.starts, 1)

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

    def test_diagnostics_do_not_claim_live_auth_from_configured_key(self):
        result = self.adapter.diagnostics(self.policy, credential())
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

    def test_oversized_event_is_rejected(self):
        with self.assertRaises(NativeSessionError):
            CodexEvents().feed(b"a" * (256 * 1024 + 1))

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
