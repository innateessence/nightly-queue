import copy
import unittest
from uuid import uuid4

from .native_evidence import EvidenceError, validate_native_evidence


def receipt():
    sha = "a" * 40
    tree = "b" * 40
    digest = "c" * 64
    return {
        "schema_version": 2,
        "execution_kind": "native",
        "run_id": str(uuid4()),
        "operation_id": str(uuid4()),
        "runner_id": 1,
        "configuration_revision": digest,
        "policy_digest": digest,
        "context": {"id": str(uuid4()), "digest": digest},
        "runtime": {
            "image_digest": "sha256:" + digest,
            "profile_id": "native-default",
            "agent_version": "codex-test",
            "session_id": "session-1",
        },
        "workspace": {
            "repository_id": 1,
            "repository": "example/repo",
            "branch": "vibearr/issue-1",
            "base_branch": "main",
            "base_revision": "d" * 40,
            "tree_sha": tree,
            "commit_sha": sha,
        },
        "validation": {
            "profile_id": "project-default",
            "profile_digest": digest,
            "image_digest": "sha256:" + digest,
            "tree_sha": tree,
            "checks": [
                {
                    "id": "tests",
                    "operation_id": str(uuid4()),
                    "exit_code": 0,
                    "timed_out": False,
                    "started_at": "2026-11-15T00:00:00+00:00",
                    "finished_at": "2026-11-15T00:01:00+00:00",
                    "log_reference": "run/log-1",
                    "log_digest": digest,
                }
            ],
        },
        "delivery": {
            "mode": "pull_request",
            "pull_request": {
                "repository_id": 1,
                "number": 2,
                "url": "https://github.com/example/repo/pull/2",
                "base": "main",
                "head": "vibearr/issue-1",
                "head_sha": sha,
            },
            "pushed_sha": sha,
        },
    }


class NativeEvidenceTests(unittest.TestCase):
    def test_native_receipt_needs_no_approval_or_separate_plan(self):
        value = receipt()
        self.assertEqual(validate_native_evidence(value), value)

    def test_legacy_fields_cannot_be_mixed_into_native_receipt(self):
        value = receipt()
        value["approval_id"] = str(uuid4())
        with self.assertRaises(EvidenceError):
            validate_native_evidence(value)

    def test_failed_validation_cannot_form_a_delivery_receipt(self):
        value = receipt()
        value["validation"]["checks"][0]["exit_code"] = 1
        with self.assertRaises(EvidenceError):
            validate_native_evidence(value)

    def test_changed_tree_invalidates_validation(self):
        value = receipt()
        value["workspace"]["tree_sha"] = "e" * 40
        with self.assertRaises(EvidenceError):
            validate_native_evidence(value)

    def test_push_must_match_validated_commit(self):
        value = receipt()
        value["delivery"]["pushed_sha"] = "e" * 40
        with self.assertRaises(EvidenceError):
            validate_native_evidence(value)

    def test_other_repository_pull_request_is_rejected(self):
        value = receipt()
        value["delivery"]["pull_request"][
            "url"
        ] = "https://github.com/other/repo/pull/2"
        with self.assertRaises(EvidenceError):
            validate_native_evidence(value)

    def test_duplicate_validation_command_cannot_replace_required_coverage(self):
        value = receipt()
        value["validation"]["checks"].append(
            copy.deepcopy(value["validation"]["checks"][0])
        )
        with self.assertRaises(EvidenceError):
            validate_native_evidence(value)

    def test_one_execution_cannot_attest_two_validation_commands(self):
        value = receipt()
        copied = copy.deepcopy(value["validation"]["checks"][0])
        copied["id"] = "other-test"
        value["validation"]["checks"].append(copied)
        with self.assertRaises(EvidenceError):
            validate_native_evidence(value)

    def test_automatic_merge_mode_cannot_form_delivery_evidence(self):
        value = receipt()
        value["delivery"]["mode"] = "merge"
        with self.assertRaises(EvidenceError):
            validate_native_evidence(value)
