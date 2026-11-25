from copy import deepcopy
from types import SimpleNamespace
import unittest
from uuid import uuid4

from vibearr_runner.delivery import DeliveryError
from vibearr_runner.factory_delivery import factory_delivery_barrier
from vibearr_runner.native_contract import digest
from vibearr_runner.policy import ExecutionPolicy
from vibearr_runner.test_factory_delivery import review_chain


class FactoryArtifactReadbackTests(unittest.TestCase):
    def setUp(self):
        self.records, binding = review_chain()
        snapshot = {"effective": {"factory": {"enabled": True}}}
        specification = {
            "role": "specification",
            "context_id": binding["context_id"],
            "payload": {"criteria": ["Criterion"]},
        }
        specification_id = str(uuid4())
        self.specification = {
            "id": specification_id,
            "document": specification,
            "digest": digest(specification),
        }
        plan = {
            "factory_specification_id": specification_id,
            "factory_specification_digest": digest(specification),
        }
        self.plan = {
            "id": binding["specification_id"],
            "document": plan,
            "digest": digest(plan),
        }
        binding.update(
            configuration_digest=digest(snapshot), specification_digest=digest(plan)
        )
        for entry in self.records:
            entry["document"].update(binding)
            entry.update(
                {
                    key: entry["document"][key]
                    for key in ("role", "round", "context_id", "specification_id")
                }
            )
            entry["digest"] = digest(entry["document"])
        self.summaries = [
            {key: value for key, value in entry.items() if key != "document"}
            for entry in self.records
        ]
        self.host = SimpleNamespace(
            run_id=binding["run_id"],
            policy=ExecutionPolicy(),
            snapshot=snapshot,
            native={
                "context": {
                    "id": binding["context_id"],
                    "digest": binding["context_digest"],
                },
                "candidate": {
                    "source_digest": binding["source_digest"],
                    "workspace": {
                        "tree_sha": binding["tree_sha"],
                        "base_revision": binding["base_sha"],
                        "commit_sha": binding["head_sha"],
                    },
                },
            },
            client=SimpleNamespace(request=self.request),
        )
        self.calls = []

    def request(self, method, path):
        self.assertEqual(method, "GET")
        self.calls.append(path)
        if "?after=" in path:
            after = int(path.rsplit("=", 1)[1])
            entries = [entry for entry in self.summaries if entry["sequence"] > after]
            page = entries[:10]
            return {
                "artifacts": page,
                "has_more": len(entries) > 10,
                "next_after": page[-1]["sequence"] if len(entries) > 10 else None,
            }
        if "/factory/artifacts/" in path:
            identity = path.rstrip("/").rsplit("/", 1)[1]
            if identity == self.specification["id"]:
                return {"artifact": self.specification}
            return {
                "artifact": next(
                    entry for entry in self.records if entry["id"] == identity
                )
            }
        if "/plans/" in path:
            return {"plan": self.plan}
        return {"run": {"plan_reference": self.plan["id"]}}

    def check(self):
        return factory_delivery_barrier(self.host)

    def test_delivery_fetches_verified_details_instead_of_trusting_summaries(self):
        self.assertEqual(self.check()["decision_id"], self.records[-1]["id"])
        self.assertEqual(
            sum(
                "/factory/artifacts/" in path and "?" not in path for path in self.calls
            ),
            6,
        )

    def test_changed_document_digest_blocks_publication(self):
        self.records[-2]["document"]["payload"]["coverage"] = []
        with self.assertRaises(DeliveryError):
            self.check()

    def test_summary_digest_must_match_exact_detail(self):
        self.summaries[-2]["digest"] = "1" * 64
        with self.assertRaises(DeliveryError):
            self.check()

    def test_summary_role_cannot_hide_a_newer_build(self):
        self.summaries[-2]["role"] = "decision"
        with self.assertRaises(DeliveryError):
            self.check()

    def test_presentation_only_details_cannot_authorize_publication(self):
        self.records[-2]["presentation_only"] = True
        with self.assertRaises(DeliveryError):
            self.check()

    def test_changed_current_plan_digest_blocks_publication(self):
        self.plan["document"]["factory_specification_digest"] = "1" * 64
        with self.assertRaises(DeliveryError):
            self.check()

    def test_later_round_is_selected_across_multiple_pages(self):
        original = deepcopy(self.records)
        for number in (2, 3):
            mapping = {entry["id"]: str(uuid4()) for entry in original}
            for entry in deepcopy(original):
                entry.update(
                    id=mapping[entry["id"]],
                    sequence=len(self.records) + 1,
                    round=number,
                )
                entry["document"]["round"] = number
                if entry["role"] == "decision":
                    entry["document"]["payload"]["artifact_ids"] = [
                        mapping[value]
                        for value in entry["document"]["payload"]["artifact_ids"]
                    ]
                entry["digest"] = digest(entry["document"])
                self.records.append(entry)
        self.summaries = [
            {key: value for key, value in entry.items() if key != "document"}
            for entry in self.records
        ]
        self.assertEqual(self.check()["round"], 3)
        self.assertTrue(any("?after=10" in path for path in self.calls))

    def test_repeated_cursor_cannot_truncate_newer_evidence(self):
        self.host.client.request = lambda method, path: (
            self.request(method, path)
            if "?" not in path
            else {"artifacts": self.summaries, "has_more": True, "next_after": 0}
        )
        with self.assertRaises(DeliveryError):
            self.check()
