import importlib.util
from copy import deepcopy
from dataclasses import asdict, replace
import json
import unittest
from unittest.mock import patch

from vibearr_runner.budgets import BudgetExceeded
from vibearr_runner.factory_generation import FactoryBaseUpdate, PublicationBaseChanged
from vibearr_runner.factory_recovery import FactoryRecovery
from vibearr_runner.recovery import ReconciliationRequired
from vibearr_runner.workspaces import git

from . import test_factory_reviews as review_fixture


SDK_AVAILABLE = importlib.util.find_spec("vibearr_executor") is not None


@unittest.skipUnless(
    SDK_AVAILABLE, "Install the native-runtime extra for native executor tests"
)
class FactoryGenerationTests(unittest.TestCase):
    def setUp(self):
        harness = review_fixture.NativeFactoryEngineTests()
        harness.setUp()
        self.addCleanup(harness.doCleanups)
        self.fixture, self.advance = harness.fixture, harness.advance
        self.advance(lambda: self.fixture.state["native"].get("phase") == "delivery")
        self.native = self.fixture.state["native"]
        self.old = deepcopy(self.native)
        workspace = self.native["candidate"]["workspace"]
        checkout = self.engine().workspace().checkout
        tree = git(["rev-parse", workspace["base_revision"] + "^{tree}"], checkout)
        self.new_base = git(
            [
                "-c",
                "user.name=fixture",
                "-c",
                "user.email=fixture@example.test",
                "commit-tree",
                tree,
                "-p",
                workspace["base_revision"],
                "-m",
                "Updated protected base",
            ],
            checkout,
        )
        self.observation = PublicationBaseChanged(workspace, self.new_base)
        git(["update-ref", "refs/heads/protected-newbase", self.new_base], checkout)

    def engine(self):
        engine = self.fixture.engine()
        engine.state = self.fixture.state
        return engine

    def recovery(self):
        engine = self.engine()
        return FactoryRecovery(engine, engine.native["factory"])

    def checkout(self, request):
        previous = self.engine().workspace()
        directory = previous.directory.parent / "new-generation"
        directory.mkdir(exist_ok=True)
        checkout = directory / "checkout"
        if not checkout.exists():
            git(
                [
                    "clone",
                    "--no-local",
                    "--no-checkout",
                    "--branch",
                    "main",
                    "--",
                    str(previous.checkout),
                    str(checkout),
                ],
                local=True,
            )
            git(["update-ref", "refs/remotes/origin/main", self.new_base], checkout)
            git(
                [
                    "checkout",
                    "--no-track",
                    "-b",
                    previous.branch_name,
                    self.new_base,
                    "--",
                ],
                checkout,
            )
        workspace = replace(previous, directory=directory, base_revision=self.new_base)
        (directory / "workspace.json").write_text(
            json.dumps({**asdict(workspace), "directory": str(directory)})
        )
        return workspace

    def change_base(self):
        self.assertEqual(self.recovery().base_changed(self.observation), "continue")
        with patch.object(FactoryBaseUpdate, "prepare_workspace", self.checkout):
            self.fixture.step()

    def test_base_generation_retains_claim_budget_history_and_reruns_every_role(self):
        spent = self.fixture.state["budgets"][self.fixture.run_id]["total"]
        started = list(self.fixture.executor.started)
        self.change_base()
        self.assertEqual(self.native["attempt"], 1)
        self.assertEqual(self.native["factory"]["round"], 2)
        self.assertEqual(self.native["factory"]["stage"], "intake")
        self.assertNotEqual(self.native["context"]["id"], self.old["context"]["id"])
        self.assertEqual(
            self.native["context"]["document"]["source"],
            self.old["context"]["document"]["source"],
        )
        self.assertEqual(
            self.fixture.state["budgets"][self.fixture.run_id]["total"], spent
        )
        self.assertNotIn("plan", self.native)
        self.assertNotIn("candidate", self.native)
        self.assertEqual(self.native["factory"]["artifacts"], {})
        self.assertEqual(
            self.native["factory_generations"][0]["candidate"], self.old["candidate"]
        )
        self.assertIn("answer.py", self.native["factory_base_changes"]["prior_diff"])
        self.advance(lambda: self.native.get("phase") == "delivery")
        new_operations = self.fixture.executor.started[len(started) :]
        roles = [
            self.fixture.executor.manifests[identity]["profile_id"]
            for identity in new_operations
        ]
        for role in ("intake", "specification", "build", "reviewer-code", "decision"):
            self.assertIn("factory-" + role, roles)
        self.assertFalse(set(started) & set(new_operations))
        self.assertNotEqual(self.native["plan"]["id"], self.old["plan"]["id"])
        prompt = self.native["factory"]["roles"]["build:2"]["request"]["prompt"]
        quoted = json.loads(prompt.split("Quoted task data: ", 1)[1])
        self.assertIn(
            self.native["factory_base_changes"]["prior_diff"],
            quoted["previous_artifacts"]["diagnostics"],
        )
        self.assertEqual(
            self.native["candidate"]["workspace"]["base_revision"], self.new_base
        )
        self.assertTrue(
            self.old["workspace"]["directory"] != self.native["workspace"]["directory"]
        )

    def test_prepared_or_unknown_publication_retains_original_branch_and_allowance(
        self,
    ):
        for delivery in (
            {"publication_id": "prepared"},
            {"push_dispatched": True},
            {"checkpoint": "pushed"},
        ):
            self.native["delivery"] = delivery
            with (
                self.subTest(delivery=delivery),
                self.assertRaisesRegex(ReconciliationRequired, "original publication"),
            ):
                self.recovery().base_changed(self.observation)
        self.assertEqual(self.native["attempt"], 0)
        self.assertEqual(self.native["workspace"], self.old["workspace"])
        self.assertNotIn("factory_base_update", self.native)

    def test_base_change_consumes_the_same_final_repair_as_review_failure(self):
        self.native["attempt"] = 1
        self.native["factory"]["round"] = 2
        self.change_base()
        self.assertEqual(self.native["attempt"], 2)
        self.assertEqual(self.recovery().data["repairs"][-1]["attempt"], 2)

    def test_base_then_real_review_changes_exhaust_one_shared_allowance(self):
        self.fixture.executor.changes_once = True
        self.change_base()
        self.advance(lambda: self.native.get("phase") == "delivery")
        self.assertEqual(self.native["attempt"], 2)
        self.assertEqual(self.native["factory"]["round"], 3)
        self.assertEqual(
            [entry["reason"] for entry in self.recovery().data["repairs"]],
            ["base_update", "reviews"],
        )
        observation = PublicationBaseChanged(
            self.native["candidate"]["workspace"], "a" * 40
        )
        self.recovery().base_changed(observation)
        with self.assertRaisesRegex(BudgetExceeded, "shared repair"):
            FactoryBaseUpdate(self.engine()).step()
        self.assertEqual(self.native["attempt"], 2)

    def test_prepared_publication_intent_blocks_generation_even_without_a_result(self):
        self.native["intents"]["uncertain-publication"] = {"kind": "delivery"}
        with self.assertRaisesRegex(ReconciliationRequired, "original identity"):
            self.recovery().base_changed(self.observation)
        self.assertEqual(self.native["attempt"], 0)
        self.assertNotIn("factory_base_update", self.native)

    def test_actual_delivery_callback_prepares_generation_before_any_publication(self):
        from vibearr_runner.native_delivery import NativeDelivery

        delivery = NativeDelivery(self.engine())
        with (
            patch.object(delivery, "verify"),
            patch.object(delivery, "clients", return_value=(object(), object())),
            patch.object(delivery, "identity", side_effect=self.observation),
        ):
            self.assertEqual(delivery.step(), "continue")
        self.assertEqual(self.native["phase"], "factory_base_update")
        self.assertEqual(self.native["attempt"], 0)
        self.assertEqual(self.native["delivery"], {})
        self.assertNotIn("publication_id", self.native["delivery"])

    def test_new_generation_cannot_remove_previously_accepted_specification_criteria(
        self,
    ):
        self.change_base()
        self.fixture.executor.results["intake"]["criteria"] = (
            self.fixture.executor.results["intake"]["criteria"][:1]
        )
        self.advance(lambda: bool(self.fixture.queue.finishes))
        self.assertIn(
            "accepted criteria", self.fixture.queue.finishes[-1]["error_summary"]
        )
        self.assertNotIn("plan", self.native)

    def test_zero_factory_repair_limit_blocks_base_generation(self):
        engine = self.engine()
        engine.snapshot["effective"]["factory"]["max_repairs"] = 0
        self.native["factory"].pop("recovery")
        self.recovery().base_changed(self.observation)
        with self.assertRaisesRegex(BudgetExceeded, "shared repair"):
            FactoryBaseUpdate(engine).cleanup(self.native["factory_base_update"])
        self.assertEqual(self.native["attempt"], 0)
        self.assertEqual(self.native["context"], self.old["context"])

    def test_exhausted_repair_budget_cannot_start_a_base_generation(self):
        self.native["attempt"] = 2
        self.native["factory"]["round"] = 3
        self.recovery().base_changed(self.observation)
        with self.assertRaisesRegex(BudgetExceeded, "shared repair"):
            FactoryBaseUpdate(self.engine()).step()
        self.assertEqual(self.native["attempt"], 2)
        self.assertEqual(self.native["context"], self.old["context"])

    def test_lost_context_acknowledgement_keeps_same_generation_and_operation(self):
        self.recovery().base_changed(self.observation)
        engine = self.engine()
        original = engine.metadata_request
        seen = []

        def lost(path, payload):
            if not path.endswith("/contexts/"):
                return original(path, payload)
            seen.append(payload["operation_id"])
            result = original(path, payload)
            if len(seen) == 1:
                raise ReconciliationRequired("Lost replacement context response")
            return result

        with (
            patch.object(FactoryBaseUpdate, "prepare_workspace", self.checkout),
            patch.object(engine, "metadata_request", lost),
        ):
            with self.assertRaisesRegex(ReconciliationRequired, "Lost replacement"):
                FactoryBaseUpdate(engine).step()
            self.assertEqual(self.native["attempt"], 1)
            FactoryBaseUpdate(engine).step()
        self.assertEqual(seen[0], seen[1])
        self.assertEqual(len(self.native["factory_generations"]), 1)
        self.assertEqual(self.native["attempt"], 1)
