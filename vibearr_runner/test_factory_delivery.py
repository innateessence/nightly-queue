from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from vibearr_runner.delivery import DeliveryError
from vibearr_runner.factory_delivery import (
    factory_delivery_barrier,
    require_review_chain,
)
from vibearr_runner.native_delivery import NativeDelivery
from vibearr_runner.policy import ExecutionPolicy


def review_chain():
    binding = {
        "run_id": str(uuid4()),
        "context_id": str(uuid4()),
        "context_digest": "a" * 64,
        "specification_id": str(uuid4()),
        "specification_digest": "b" * 64,
        "source_digest": "c" * 64,
        "tree_sha": "d" * 40,
        "base_sha": "e" * 40,
        "head_sha": "f" * 40,
        "configuration_digest": "0" * 64,
    }
    records = []
    for role, payload in (
        ("build", {"summary": "Implemented criterion-1", "criteria": ["criterion-1"]}),
        ("checks", {"state": "passed", "validation_id": str(uuid4())}),
        ("routing", {"required_reviewers": ["reviewer-code"], "surfaces": []}),
        (
            "reviewer-code",
            {"verdict": "pass", "coverage": ["criterion-1"], "findings": []},
        ),
        (
            "decision",
            {
                "verdict": "ready",
                "artifact_ids": [],
                "rationale": "Checks and review passed",
            },
        ),
    ):
        document = {**binding, "role": role, "round": 1, "payload": payload}
        if role in {"build", "reviewer-code", "decision"}:
            document["execution"] = {
                field: str(uuid4())
                for field in ("session_id", "operation_id", "intent_id", "result_id")
            }
        records.append(
            {"id": str(uuid4()), "sequence": len(records) + 1, "document": document}
        )
    records[-1]["document"]["payload"]["artifact_ids"] = [
        entry["id"] for entry in records[1:-1]
    ]
    return records, binding


class FactoryDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.records, self.binding = review_chain()

    def check(self):
        return require_review_chain(self.records, self.binding, ["criterion-1"])

    def test_current_complete_chain_permits_only_its_ready_decision(self):
        self.assertEqual(self.check()["decision_id"], self.records[-1]["id"])

    def test_missing_review_prevents_publication(self):
        del self.records[-2]
        with self.assertRaises(DeliveryError):
            self.check()

    def test_wrong_candidate_tree_prevents_publication(self):
        self.records[-2]["document"]["tree_sha"] = "1" * 40
        with self.assertRaises(DeliveryError):
            self.check()

    def test_current_review_changes_invalidate_old_ready_decision(self):
        latest = deepcopy(self.records[-2])
        latest.update(id=str(uuid4()), sequence=6)
        latest["document"]["payload"]["verdict"] = "changes"
        self.records.append(latest)
        with self.assertRaises(DeliveryError):
            self.check()

    def test_new_build_cannot_reuse_previous_round_reviews(self):
        latest = deepcopy(self.records[0])
        latest.update(id=str(uuid4()), sequence=6)
        latest["document"]["round"] = 2
        self.records.append(latest)
        with self.assertRaises(DeliveryError):
            self.check()

    def test_required_security_review_cannot_be_omitted(self):
        self.records[2]["document"]["payload"]["surfaces"] = ["security"]
        with self.assertRaises(DeliveryError):
            self.check()

    def test_unresolved_decision_cannot_authorize_delivery(self):
        self.records[-1]["document"]["payload"]["verdict"] = "unresolved"
        with self.assertRaises(DeliveryError):
            self.check()

    def test_decision_cannot_waive_or_reference_a_different_review(self):
        self.records[-1]["document"]["payload"]["artifact_ids"][-1] = str(uuid4())
        with self.assertRaises(DeliveryError):
            self.check()

    def test_review_session_must_be_independent_of_builder(self):
        self.records[-2]["document"]["execution"]["session_id"] = self.records[0][
            "document"
        ]["execution"]["session_id"]
        with self.assertRaises(DeliveryError):
            self.check()

    def test_code_review_covers_every_acceptance_criterion(self):
        self.records[-2]["document"]["payload"]["coverage"] = []
        with self.assertRaises(DeliveryError):
            self.check()

    def test_merge_mode_rejected_before_any_publication_or_merge_call(self):
        host = SimpleNamespace(
            native={}, policy=replace(ExecutionPolicy(), delivery_mode="merge")
        )
        delivery = NativeDelivery(host)
        delivery.clients = Mock()
        with self.assertRaises(DeliveryError):
            delivery.step()
        delivery.clients.assert_not_called()

    def test_direct_merge_entrypoint_never_dispatches_provider_write(self):
        delivery = NativeDelivery(SimpleNamespace(native={}))
        with self.assertRaises(DeliveryError):
            delivery.merge()

    def test_nonfactory_pr_delivery_does_not_require_factory_artifacts(self):
        host = SimpleNamespace(policy=ExecutionPolicy(), snapshot={})
        self.assertIsNone(factory_delivery_barrier(host))

    def test_lost_finish_response_reads_completed_receipt_without_reauthorizing_publication(
        self,
    ):
        url = "https://github.com/owner/repo/pull/17"
        host = SimpleNamespace(
            native={"delivery": {"pull_request": {"url": url}}},
            run_id=str(uuid4()),
            policy=ExecutionPolicy(),
            client=Mock(),
        )
        host.client.reconcile.return_value = {
            "run": {
                "state": "completed",
                "result_status": "succeeded",
                "result_url": url,
                "native_outcome": "delivered",
            }
        }
        with patch(
            "vibearr_runner.native_delivery.factory_delivery_barrier"
        ) as barrier:
            self.assertEqual(NativeDelivery(host).report(), "reporting_pending")
        barrier.assert_not_called()
        host.client.request.assert_not_called()
