"""Provider-neutral fixture cases; these do not establish live acceptance."""

import copy
from datetime import datetime, timedelta, timezone
import hashlib
import json
from uuid import uuid4

from vibearr_runner.native_sessions import NativeSession, NativeSessionError
from vibearr_runner.policy import default_execution_policy


class ExecutorFixture:
    def __init__(self, profile):
        self.profile = profile
        self.prepared = {}
        self.starts = 0
        self.uploads = []
        self.secrets = {}
        self.content = b""
        self.state = "not-started"
        self.receipt = None
        self.qualified = True
        self.binding_digest = "b" * 64
        self.export_unchanged = False

    def capabilities(self):
        from vibearr_executor import QUALIFICATION_VERSION

        return {
            "runtime": "docker-oci-v1",
            "schema_version": 1,
            "protocol_version": 1,
            "qualification_version": QUALIFICATION_VERSION,
            "binding_digest": self.binding_digest,
            "qualified": self.qualified,
            "accepting_work": True,
            "profiles": [
                {**self.profile.data(), "profile_digest": self.profile.digest}
            ],
        }

    def upload(self, kind, content):
        self.uploads.append((kind, content))
        return {"sha256": hashlib.sha256(content).hexdigest()}

    def prepare(self, manifest):
        from vibearr_executor.contracts import Conflict

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
            "container_id": "owned-container",
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

    def complete(self, operation, *, exit_code=0):
        self.state = "exited"
        self.receipt = {
            "run_id": self.prepared[operation]["run_id"],
            "operation_id": operation,
            "exit_code": exit_code,
            "timed_out": False,
            "canceled": False,
            "log_truncated": False,
            "log_digest": hashlib.sha256(self.content).hexdigest(),
            "ended_at": datetime.now(timezone.utc).timestamp(),
        }

    def export(self, operation, source):
        from vibearr_executor.artifacts import pack, result_manifest

        exported = (
            source
            if self.export_unchanged
            else pack({"answer.py": (0o644, b"answer = 42\n")})
        )
        return result_manifest(source, exported), exported


class NativeAdapterConformance:
    def configure_adapter(self):
        raise NotImplementedError

    def completion(self):
        raise NotImplementedError

    def expected_usage(self):
        return {"input_tokens": 12, "output_tokens": 4}

    def setUp(self):
        from vibearr_executor.artifacts import pack

        self.configure_adapter()
        self.policy = default_execution_policy()
        self.policy["runtime_profile"] = self.client.profile.profile_id
        self.policy["network_hosts"] = list(self.client.profile.network_hosts)
        self.source = pack({"answer.py": (0o644, b"answer = 0\n")})
        self.request = {
            "run_id": str(uuid4()),
            "operation_id": str(uuid4()),
            "source": self.source,
            "prompt": "Set answer to42 and run its tests.",
            "model": self.model,
            "policy": self.policy,
            "deadline": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
            "credential": self.credential,
        }

    def prepare(self):
        return self.adapter.prepare(**self.request)

    def finish(self, session):
        self.client.content = self.completion()
        self.client.complete(session.manifest["operation_id"])
        return self.adapter.outcome(session, self.source)

    def proof(self):
        request = {
            key: value
            for key, value in self.request.items()
            if key not in {"source", "prompt"}
        }
        session = self.adapter.prepare_auth_probe(**request)
        self.client.content = self.completion()
        self.client.export_unchanged = True
        self.client.complete(session.manifest["operation_id"])
        return self.adapter.authentication_result(session)

    def test_prepare_binds_provider_model_credential_and_manifest_without_secret(self):
        session = self.prepare()
        self.assertEqual(session.adapter, self.adapter.provider)
        self.assertEqual(
            session.account,
            {
                "backend": self.adapter.provider,
                "credential_id": 7,
                "credential_version": 2,
                "model": self.model,
            },
        )
        self.assertEqual(
            self.client.prepared[session.manifest["operation_id"]], session.manifest
        )
        self.assertNotIn(self.secret, json.dumps(session.to_data()))
        self.assertEqual(self.client.starts, 0)

    def test_missing_auth_cannot_create_artifacts_or_fall_back(self):
        self.request["credential"] = {}
        with self.assertRaises(NativeSessionError):
            self.prepare()
        self.assertFalse(self.client.prepared)
        self.assertFalse(self.client.uploads)

    def test_wrong_provider_cannot_create_operation(self):
        self.request["credential"]["credential"]["provider"] = "foreign"
        with self.assertRaises(NativeSessionError):
            self.prepare()
        self.assertFalse(self.client.prepared)

    def test_secret_cannot_enter_prompt(self):
        self.request["prompt"] += self.secret
        with self.assertRaises(NativeSessionError):
            self.prepare()

    def test_unqualified_runtime_cannot_prepare(self):
        self.client.qualified = False
        with self.assertRaises(NativeSessionError):
            self.prepare()

    def test_model_network_cannot_expand_policy(self):
        self.policy["network_hosts"] = []
        with self.assertRaises(NativeSessionError):
            self.prepare()

    def test_lost_start_reply_reconciles_same_container_and_invocation(self):
        session = self.prepare()
        first = self.adapter.start(session)
        restored = NativeSession.from_data(json.loads(json.dumps(session.to_data())))
        observed = type(self.adapter)(self.client).reconcile(restored)
        self.assertEqual(observed["container_id"], first["container_id"])
        self.adapter.start(restored)
        self.assertEqual(self.client.starts, 1)

    def test_changed_runtime_blocks_start_but_allows_stop_and_reconcile(self):
        session = self.prepare()
        self.adapter.start(session)
        self.client.binding_digest = "c" * 64
        with self.assertRaises(NativeSessionError):
            self.adapter.start(session)
        self.assertEqual(self.adapter.reconcile(session)["state"], "running")
        self.assertTrue(self.adapter.cancel(session)["descendants_terminated"])
        self.assertEqual(self.client.starts, 1)

    def test_unknown_execution_has_no_outcome_or_replacement_start(self):
        session = self.prepare()
        self.client.state = "unknown"
        self.assertEqual(self.adapter.reconcile(session)["state"], "unknown")
        with self.assertRaises(NativeSessionError):
            self.adapter.outcome(session, self.source)
        self.assertEqual(self.client.starts, 0)

    def test_restored_session_cannot_switch_provider(self):
        record = self.prepare().to_data()
        record["adapter"] = "foreign"
        with self.assertRaises(NativeSessionError):
            self.adapter.reconcile(NativeSession.from_data(record))

    def test_changed_manifest_cannot_reuse_operation(self):
        from vibearr_executor.contracts import Conflict

        record = self.prepare().to_data()
        record["manifest"]["context_digest"] = "f" * 64
        with self.assertRaises(Conflict):
            self.adapter.reconcile(NativeSession.from_data(record))

    def test_protected_success_exports_source_with_genuine_token_units(self):
        outcome = self.finish(self.prepare())
        self.assertTrue(outcome["successful"])
        self.assertEqual(outcome["usage"], self.expected_usage())
        self.assertEqual(outcome["usage_unit"], "tokens")
        self.assertFalse(outcome["quota_supported"])

    def test_process_exit_without_agent_completion_is_not_success(self):
        session = self.prepare()
        self.client.complete(session.manifest["operation_id"])
        self.assertFalse(self.adapter.outcome(session, self.source)["successful"])

    def test_canceled_session_is_not_success_despite_completion_output(self):
        session = self.prepare()
        self.adapter.start(session)
        self.client.content = self.completion()
        self.adapter.cancel(session)
        self.assertFalse(self.adapter.outcome(session, self.source)["successful"])

    def test_wrong_run_receipt_is_rejected(self):
        session = self.prepare()
        self.client.complete(session.manifest["operation_id"])
        self.client.receipt["run_id"] = str(uuid4())
        with self.assertRaises(NativeSessionError):
            self.adapter.inspect(session)

    def test_tampered_log_cannot_prove_success(self):
        session = self.prepare()
        self.client.content = self.completion()
        self.client.complete(session.manifest["operation_id"])
        self.client.receipt["log_digest"] = "f" * 64
        with self.assertRaises(NativeSessionError):
            self.adapter.outcome(session, self.source)

    def test_truncated_completion_output_cannot_prove_success(self):
        session = self.prepare()
        self.client.content = self.completion()
        self.client.complete(session.manifest["operation_id"])
        self.client.receipt["log_truncated"] = True
        self.assertFalse(self.adapter.outcome(session, self.source)["successful"])

    def test_selected_key_presence_is_not_authenticated_readiness(self):
        result = self.adapter.diagnostics(
            self.policy, self.credential, model=self.model
        )
        self.assertTrue(result["auth_available"])
        self.assertFalse(result["authenticated"])
        self.assertFalse(result["native_agent_ready"])

    def test_auth_proof_binds_actual_selected_model_and_credential_version(self):
        proof = self.proof()
        result = self.adapter.diagnostics(
            self.policy, self.credential, model=self.model, proof=proof
        )
        self.assertTrue(result["authenticated"])
        self.assertEqual(proof["account"]["model"], self.model)
        changed = copy.deepcopy(self.credential)
        changed["credential"]["version"] += 1
        self.assertFalse(
            self.adapter.diagnostics(
                self.policy, changed, model=self.model, proof=proof
            )["authenticated"]
        )

    def test_another_model_cannot_reuse_auth_proof(self):
        proof = self.proof()
        self.assertFalse(
            self.adapter.diagnostics(
                self.policy, self.credential, model=self.model + "-other", proof=proof
            )["authenticated"]
        )

    def test_regular_task_cannot_mint_authentication_proof(self):
        with self.assertRaises(NativeSessionError):
            self.adapter.authentication_result(self.prepare())

    def test_expired_probe_cannot_renew_readiness(self):
        proof = self.proof()
        proof["verified_at"] = (
            datetime.now(timezone.utc) - timedelta(hours=1)
        ).isoformat()
        proof["expires_at"] = (
            datetime.now(timezone.utc) - timedelta(minutes=45)
        ).isoformat()
        self.assertFalse(
            self.adapter.diagnostics(
                self.policy, self.credential, model=self.model, proof=proof
            )["authenticated"]
        )
