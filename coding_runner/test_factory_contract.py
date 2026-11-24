import copy
import unittest
from uuid import uuid4

from vibearr_runner.factory_contract import (
    FactoryContractError,
    artifact_document,
    repair_limit,
    validate_artifact,
    validate_role_intent,
)


def artifact(role="intake"):
    payloads = {
        "intake": {
            "verdict": "accept",
            "scope": "Requested work",
            "criteria": ["Works"],
            "risk": "low",
            "surfaces": [],
            "rationale": "Clear request",
            "questions": [],
        },
        "specification": {
            "scope": "Requested work",
            "criteria": [{"id": "works", "description": "Works"}],
            "verification": ["Run a real check"],
            "exclusions": [],
        },
        "build": {
            "summary": "Changed requested behavior",
            "criteria": {"works": "Implemented"},
        },
        "routing": {
            "risk": "medium",
            "surfaces": ["ui"],
            "required_reviewers": ["reviewer-code", "reviewer-ui", "reviewer-ux"],
            "rule_digest": "a" * 64,
        },
        "checks": {"validation_id": str(uuid4()), "state": "failed"},
        "reviewer-code": {
            "verdict": "changes",
            "findings": [
                {
                    "id": "missing-case",
                    "summary": "Missing case",
                    "evidence": ["file.py:10"],
                    "criterion_ids": ["works"],
                }
            ],
            "coverage": ["works"],
        },
        "decision": {
            "verdict": "unresolved",
            "rationale": "Review needs changes",
            "artifact_ids": [str(uuid4())],
        },
    }
    value = {
        "run_id": str(uuid4()),
        "operation_id": str(uuid4()),
        "role": role,
        "round": 1,
        "context_id": str(uuid4()),
        "context_digest": "a" * 64,
        "specification_id": (
            None if role in {"intake", "specification"} else str(uuid4())
        ),
        "specification_digest": "" if role in {"intake", "specification"} else "a" * 64,
        "source_digest": "b" * 64,
        "tree_sha": "c" * 40,
        "base_sha": "d" * 40,
        "head_sha": "",
        "configuration_digest": "e" * 64,
        "payload": payloads[role],
    }
    if role not in {"checks", "routing"}:
        value["execution"] = {
            "intent_id": str(uuid4()),
            "result_id": str(uuid4()),
            "adapter": "codex",
            "model": "configured-model",
            "agent_version": "pinned-cli",
            "session_id": "session-1",
            "operation_id": str(uuid4()),
            "execution_identity": {
                "runtime": "compose-slot-v2",
                "protocol_version": 2,
                "instance_id": str(uuid4()),
                "worker_boot": str(uuid4()),
                "binding_digest": "a" * 64,
            },
            "input_source_digest": "b" * 64,
            "input_digest": "a" * 64,
            "prompt_digest": "f" * 64,
            "started_at": 1.0,
            "ended_at": 2.0,
        }
    return artifact_document(**value)


class FactoryContractTests(unittest.TestCase):
    def test_role_payloads_preserve_actual_nonpassing_outcomes(self):
        for role in (
            "intake",
            "specification",
            "build",
            "routing",
            "checks",
            "reviewer-code",
            "decision",
        ):
            with self.subTest(role=role):
                value = artifact(role)
                self.assertEqual(validate_artifact(value), value)

    def test_validated_documents_are_copies(self):
        value = artifact()
        saved = validate_artifact(value)
        value["payload"]["scope"] = "Changed afterwards"
        self.assertEqual(saved["payload"]["scope"], "Requested work")

    def test_code_review_cannot_be_removed(self):
        value = artifact("routing")
        value["payload"]["required_reviewers"] = ["reviewer-ui"]
        with self.assertRaises(FactoryContractError):
            validate_artifact(value)

    def test_dispatch_role_and_round_are_bounded(self):
        value = {
            "role": "reviewer-code",
            "round": 1,
            "prompt_digest": "a" * 64,
            "input_digest": "b" * 64,
        }
        self.assertEqual(validate_role_intent(value), value)
        for change in (
            {"role": "checks"},
            {"round": True},
            {"prompt_digest": "unknown"},
            {"extra": True},
        ):
            with self.subTest(change=change), self.assertRaises(FactoryContractError):
                validate_role_intent({**value, **change})

    def test_native_execution_requires_original_worker_identity(self):
        value = artifact()
        value["execution"]["execution_identity"] = None
        with self.assertRaises(FactoryContractError):
            validate_artifact(value)

    def test_execution_timestamps_reject_huge_integers_without_overflow(self):
        for key in ("started_at", "ended_at"):
            value = artifact()
            value["execution"][key] = 10**1000
            with self.subTest(key=key), self.assertRaises(FactoryContractError):
                validate_artifact(value)

    def test_bound_dispatch_preserves_specification_identity(self):
        value = {
            "role": "build",
            "round": 1,
            "prompt_digest": "a" * 64,
            "input_digest": "b" * 64,
            "specification_id": str(uuid4()),
            "specification_digest": "c" * 64,
        }
        self.assertEqual(validate_role_intent(value), value)
        for change in (
            {"specification_id": "invalid"},
            {"specification_digest": "invalid"},
            {"specification_id": None},
        ):
            with self.subTest(change=change), self.assertRaises(FactoryContractError):
                validate_role_intent({**value, **change})
        value.pop("specification_id")
        with self.assertRaises(FactoryContractError):
            validate_role_intent(value)

    def test_bootstrap_dispatch_preserves_explicit_unbound_specification(self):
        value = {
            "role": "intake",
            "round": 1,
            "prompt_digest": "a" * 64,
            "input_digest": "b" * 64,
            "specification_id": None,
            "specification_digest": "",
        }
        self.assertEqual(validate_role_intent(value), value)

    def test_duplicate_criterion_ids_are_rejected(self):
        value = artifact("specification")
        value["payload"]["criteria"] *= 2
        with self.assertRaises(FactoryContractError):
            validate_artifact(value)

    def test_intake_needs_information_requires_a_question(self):
        value = artifact()
        value["payload"].update(verdict="needs_info", questions=[])
        with self.assertRaises(FactoryContractError):
            validate_artifact(value)

    def test_unknown_envelope_fields_and_nonfinite_numbers_are_rejected(self):
        original = artifact()
        for change in (
            {"arbitrary": True},
            {"round": float("nan")},
            {"round": float("inf")},
        ):
            with self.subTest(change=change), self.assertRaises(FactoryContractError):
                validate_artifact({**copy.deepcopy(original), **change})


class FactoryRepairLimitTests(unittest.TestCase):
    def test_frozen_factory_limit_caps_the_policy(self):
        for configured in (0, 1, 2):
            with self.subTest(configured=configured):
                snapshot = {
                    "effective": {
                        "factory": {"enabled": True, "max_repairs": configured}
                    }
                }
                self.assertEqual(repair_limit(snapshot, 2), configured)

    def test_policy_and_absolute_ceiling_still_apply(self):
        snapshot = {"effective": {"factory": {"enabled": True, "max_repairs": 10}}}
        self.assertEqual(repair_limit(snapshot, 1), 1)
        self.assertEqual(repair_limit(snapshot, 10), 2)

    def test_historical_snapshots_without_factory_limits_keep_policy(self):
        self.assertEqual(repair_limit({}, 1), 1)
        self.assertEqual(
            repair_limit({"effective": {"factory": {"enabled": True}}}, 2), 2
        )

    def test_invalid_frozen_limit_fails_closed(self):
        for configured in (True, -1, 11, "0", None):
            with self.subTest(configured=configured):
                snapshot = {
                    "effective": {
                        "factory": {"enabled": True, "max_repairs": configured}
                    }
                }
                with self.assertRaises(FactoryContractError):
                    repair_limit(snapshot, 2)
