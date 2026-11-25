import hashlib
import importlib.util
import unittest
from uuid import uuid4
from unittest.mock import patch

from vibearr_runner.native_contract import (
    NativeContractError,
    agent_result,
    context_document,
    digest,
    profile_from_reference,
    profile_reference,
    receipt_passed,
    receipt_summary,
    status_document,
    termination_record,
    validate_context,
    validate_native_artifact,
    validate_receipt,
)
from vibearr_runner.native_sessions import NativeSession

SDK_AVAILABLE = importlib.util.find_spec("vibearr_executor") is not None
if SDK_AVAILABLE:
    from vibearr_executor.artifacts import pack, result_manifest
    from vibearr_executor.validation_profile import ValidationProfile


def native_context():
    profile = ValidationProfile("sha256:" + "a" * 64, "sha256:" + "b" * 64, "c" * 64)
    return context_document(
        run_id=str(uuid4()),
        source={"title": "A task", "body": "Implement the task", "comments": []},
        repository={
            "repository_id": 99,
            "repository": "owner/repo",
            "repository_url": "https://github.com/owner/repo.git",
            "base_branch": "main",
            "base_revision": "d" * 40,
            "base_tree_sha": "e" * 40,
            "branch": "vibearr/issue-1",
        },
        source_digest="f" * 64,
        policy_digest="a" * 64,
        account={
            "backend": "opencode",
            "credential_id": 1,
            "credential_version": 1,
            "model": "provider/model",
        },
        validation_profile=profile,
        runtime_binding_digest="b" * 64,
        acceptance_criteria=["Pass required validation."],
    )


def observed_operation():
    original = pack({"file.py": (0o644, b"before\n")})
    changed = pack({"file.py": (0o644, b"after\n")})
    run_id, operation_id = str(uuid4()), str(uuid4())
    result = {
        "schema_version": 1,
        "run_id": run_id,
        "operation_id": operation_id,
        "terminated": True,
        "exit_code": 0,
        "timed_out": False,
        "canceled": False,
        "log_truncated": False,
        "started_at": 1700000000.0,
        "ended_at": 1700000001.0,
        "log_digest": hashlib.sha256(b"events").hexdigest(),
        "artifact": result_manifest(original, changed),
    }
    identity = {
        "runtime": "compact-worker-v1",
        "protocol_version": 2,
        "instance_id": str(uuid4()),
        "worker_boot": str(uuid4()),
        "binding_digest": "b" * 64,
    }
    result["execution_identity"] = identity
    observed = {
        "execution_identity": identity,
        "worker_boot": identity["worker_boot"],
        "state": "exited",
        "operation_id": operation_id,
        "descendants_terminated": True,
        "container_id": "c" * 64,
        "result": result,
    }
    return original, changed, observed


@unittest.skipUnless(
    SDK_AVAILABLE, "Install the native-runtime extra for executor-backed contract tests"
)
class NativeExecutorContractTests(unittest.TestCase):
    def test_safe_profile_round_trip_has_no_secret_selector(self):
        value = native_context()["validation_profile"]
        self.assertEqual(profile_reference(profile_from_reference(value)), value)
        self.assertNotIn("executor_profiles", value)
        self.assertNotIn("backend_secret_env", repr(value))

    def test_profile_cannot_omit_a_required_check(self):
        value = native_context()["validation_profile"]
        value["required_checks"].pop()
        with self.assertRaises(NativeContractError):
            profile_from_reference(value)

    def test_context_copies_frozen_source(self):
        value = native_context()
        saved = validate_context(value)
        value["source"]["body"] = "Changed later"
        self.assertEqual(saved["source"]["body"], "Implement the task")

    def test_context_rejects_direct_base_delivery(self):
        value = native_context()
        value["repository"]["branch"] = "main"
        with self.assertRaises(NativeContractError):
            validate_context(value)

    def test_criteria_total_size_is_bounded(self):
        value = native_context()
        value["acceptance_criteria"] = ["a" * 4000] * 17
        with self.assertRaises(NativeContractError):
            validate_context(value)

    def test_receipt_rejects_boolean_success_code(self):
        _, _, observed = observed_operation()
        observed["result"]["exit_code"] = False
        with self.assertRaises(NativeContractError):
            receipt_summary(observed)

    def test_receipt_requires_confirmed_descendants(self):
        _, _, observed = observed_operation()
        observed["descendants_terminated"] = False
        with self.assertRaises(NativeContractError):
            receipt_summary(observed)

    def test_receipt_summary_retains_digests_not_file_inventory(self):
        _, _, observed = observed_operation()
        summary = receipt_summary(observed)
        self.assertTrue(receipt_passed(summary))
        self.assertEqual(summary["receipt_digest"], digest(observed["result"]))
        self.assertNotIn("files", repr(summary))

    def test_docker_slot_receipt_passes_without_inventing_a_container_id(self):
        _, _, observed = observed_operation()
        identity = {
            "runtime": "compose-slot-v2",
            "protocol_version": 2,
            "instance_id": str(uuid4()),
            "worker_boot": str(uuid4()),
            "binding_digest": "d" * 64,
        }
        observed.pop("container_id")
        observed.update(
            execution_identity=identity, worker_boot=identity["worker_boot"]
        )
        observed["result"]["execution_identity"] = identity
        receipt = receipt_summary(observed)
        self.assertTrue(receipt_passed(receipt))
        self.assertEqual(receipt["schema_version"], 2)
        self.assertNotIn("container_id", receipt)

    def test_docker_result_cannot_be_relabelled_as_another_worker_boot(self):
        _, _, observed = observed_operation()
        identity = {
            "runtime": "compose-slot-v2",
            "protocol_version": 2,
            "instance_id": str(uuid4()),
            "worker_boot": str(uuid4()),
            "binding_digest": "d" * 64,
        }
        observed.update(execution_identity=identity, worker_boot=str(uuid4()))
        observed["result"]["execution_identity"] = identity
        with self.assertRaisesRegex(NativeContractError, "observed worker boot"):
            receipt_summary(observed)

    def test_container_id_cannot_replace_protected_worker_identity(self):
        _, _, observed = observed_operation()
        observed.pop("execution_identity")
        observed.pop("worker_boot")
        observed["result"].pop("execution_identity")
        with self.assertRaises(NativeContractError):
            receipt_summary(observed)

    def test_container_only_receipt_format_is_rejected(self):
        _, _, observed = observed_operation()
        value = receipt_summary(observed)
        value.pop("schema_version")
        value.pop("execution_identity")
        value["container_id"] = "c" * 64
        with self.assertRaises(NativeContractError):
            validate_receipt(value)

    def test_cancelled_unstarted_operation_keeps_protected_terminal_receipt(self):
        _, _, observed = observed_operation()
        observed["result"].update(
            exit_code=None, started_at=None, canceled=True, artifact=None
        )
        value = receipt_summary(observed)
        self.assertEqual(validate_receipt(value), value)
        self.assertFalse(receipt_passed(value))

    def test_agent_result_accepts_generic_session_and_usage(self):
        original, changed, observed = observed_operation()
        session = NativeSession(
            "opencode",
            "1.2.3",
            {
                "backend": "opencode",
                "credential_id": 1,
                "credential_version": 1,
                "model": "provider/model",
            },
            "connection",
            "sha256:" + "a" * 64,
            "b" * 64,
            {
                "run_id": observed["result"]["run_id"],
                "operation_id": observed["operation_id"],
            },
            observed["execution_identity"],
        )
        outcome = {
            "result": observed["result"],
            "artifact": result_manifest(original, changed),
            "source": changed,
            "successful": True,
            "session_id": "ses_real-provider-identity",
            "agent_version": "1.2.3",
            "usage": {"actual_provider_units": 12},
            "usage_unit": "tokens",
        }
        value = agent_result(
            intent_id=str(uuid4()), session=session, outcome=outcome, observed=observed
        )
        self.assertEqual(value["session_id"], "ses_real-provider-identity")
        self.assertEqual(
            value["usage"],
            [{"name": "actual_provider_units", "value": 12, "unit": "tokens"}],
        )

    def test_agent_result_rejects_export_mismatch(self):
        _, changed, observed = observed_operation()
        session = {
            "manifest": {"operation_id": observed["operation_id"]},
            "execution_identity": observed["execution_identity"],
        }
        outcome = {
            "result": observed["result"],
            "artifact": observed["result"]["artifact"],
            "source": changed + b"x",
            "successful": True,
            "session_id": "ses_123",
            "agent_version": "1.2.3",
        }
        with self.assertRaisesRegex(NativeContractError, "export bytes"):
            agent_result(
                intent_id=str(uuid4()),
                session=session,
                outcome=outcome,
                observed=observed,
            )


class NativeContractTests(unittest.TestCase):
    def test_status_accepts_provider_text_session_id(self):
        value = status_document(
            sequence=1, stage="implementation", session_id="ses_provider-123"
        )
        self.assertEqual(value["session_id"], "ses_provider-123")

    def test_status_rejects_boolean_sequence(self):
        with self.assertRaises(NativeContractError):
            status_document(sequence=True, stage="implementation")

    def test_unknown_publication_cannot_confirm_termination(self):
        value = {
            "intent_id": str(uuid4()),
            "publication_id": str(uuid4()),
            "stage": "create_pr",
            "state": "unknown",
            "pushed_sha": "",
            "pull_request": None,
        }
        with self.assertRaises(NativeContractError):
            termination_record(
                intent_ids=[], operations=[], local_processes={}, publications=[value]
            )

    def test_unknown_build_cannot_confirm_termination(self):
        build = {
            "build_id": str(uuid4()),
            "run_id": str(uuid4()),
            "state": "unknown",
            "resources_terminated": False,
            "receipt_digest": "a" * 64,
        }
        with self.assertRaises(NativeContractError):
            termination_record(
                intent_ids=[],
                operations=[],
                local_processes={},
                publications=[],
                builds=[build],
            )

    def test_invalid_native_artifact_is_rejected(self):
        with self.assertRaises(NativeContractError):
            validate_native_artifact("native_workspace", {"schema_version": 1})

    def test_missing_optional_sdk_has_an_actionable_diagnostic(self):
        original_import = __import__

        def without_sdk(name, *args, **kwargs):
            if name.startswith("vibearr_executor"):
                raise ModuleNotFoundError(
                    "Executor SDK is intentionally absent", name="vibearr_executor"
                )
            return original_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=without_sdk):
            with self.assertRaisesRegex(
                NativeContractError, r"vibearr-runner\[native-runtime\]"
            ):
                profile_from_reference({})
