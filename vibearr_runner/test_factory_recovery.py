from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

from vibearr_runner.budgets import BudgetExceeded
from vibearr_runner.factory_recovery import FactoryRecovery
from vibearr_runner.journal import Journal
from vibearr_runner.native_contract import digest
from vibearr_runner.recovery import ReconciliationRequired


class Clock:
    now = 1000.0

    def __call__(self):
        return self.now


class Executor:
    def __init__(self, run_id, clock):
        self.run_id, self.clock = run_id, clock
        self.states, self.manifests = {}, {}
        self.identity = {
            "runtime": "compact-worker-v1",
            "protocol_version": 2,
            "instance_id": str(uuid4()),
            "worker_boot": str(uuid4()),
            "binding_digest": "b" * 64,
        }
        self.stopped = []
        self.uncertain = False

    def status(self, operation_id):
        return deepcopy(self.states[operation_id])

    def termination_receipt(self, operation_id):
        return {
            "operation_id": operation_id,
            "run_id": self.run_id,
            "state": self.states[operation_id]["state"],
            "manifest_digest": digest(self.manifests[operation_id]),
            "instance_id": self.identity["instance_id"],
        }

    def exited(self, operation_id, *, started=1000.0, ended=None, canceled=False):
        return {
            "operation_id": operation_id,
            "state": "exited",
            "execution_identity": deepcopy(self.identity),
            "worker_boot": self.identity["worker_boot"],
            "descendants_terminated": True,
            "container_id": "a" * 64,
            "result": {
                "operation_id": operation_id,
                "run_id": self.run_id,
                "execution_identity": deepcopy(self.identity),
                "terminated": True,
                "exit_code": 0,
                "started_at": started,
                "ended_at": self.clock() if ended is None else ended,
                "canceled": canceled,
            },
        }

    def stop(self, operation_id):
        self.stopped.append(operation_id)
        if self.uncertain:
            return {"operation_id": operation_id, "state": "unknown"}
        self.states[operation_id] = self.exited(operation_id, canceled=True)
        return self.status(operation_id)


class Resources:
    def reconcile(self, run_id, executor, *, stop=False):
        if not stop:
            raise AssertionError("Test expects confirmed cleanup")
        return {
            "operations": [executor.stop(identity) for identity in executor.states],
            "builds": [],
        }


class Native:
    def __init__(self, journal, clock, *, state=None):
        self.journal, self.clock = journal, clock
        self.run_id = str(uuid4()) if state is None else state["native"]["run_id"]
        account = {
            "backend": "codex",
            "credential_id": 1,
            "credential_version": 1,
            "model": "test",
        }
        self.state = state or {
            "version": 1,
            "operation": None,
            "native": {
                "run_id": self.run_id,
                "attempt": 0,
                "phase": "factory",
                "deadline": self.deadline(120),
                "context": {
                    "id": str(uuid4()),
                    "digest": "d" * 64,
                    "document": {"account": account},
                },
                "plan": {"id": str(uuid4()), "digest": "b" * 64},
                "candidate": {
                    "source_digest": "c" * 64,
                    "workspace": {
                        "commit_sha": "e" * 40,
                        "base_revision": "f" * 40,
                        "tree_sha": "d" * 40,
                    },
                },
                "validation": {"result_id": str(uuid4())},
                "factory": {
                    "schema_version": 1,
                    "round": 1,
                    "roles": {},
                    "artifacts": {},
                },
            },
        }
        self.native = self.state["native"]
        self.snapshot = {
            "global": {
                "task_timeout_seconds": 120,
                "research_timeout_seconds": 15,
                "implementation_timeout_seconds": 30,
                "validation_timeout_seconds": 60,
            }
        }
        self.policy = SimpleNamespace(repair_limit=2)
        self.executor = Executor(self.run_id, clock)
        self.resources = Resources()
        self.allowed = True

    def deadline(self, seconds):
        return datetime.fromtimestamp(self.clock() + seconds, timezone.utc).isoformat()

    def save(self):
        self.journal.save(self.state)

    def checkpoint(self, *, force_source=False):
        if not force_source:
            raise AssertionError("Check live ownership/source before dispatch")
        return self.allowed


class FactoryRecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.journal = Journal(Path(temporary.name) / "journal")
        self.journal.__enter__()
        self.addCleanup(self.journal.__exit__, None, None, None)
        self.clock = Clock()
        self.native = Native(self.journal, self.clock)
        self.factory = self.native.native["factory"]
        self.recovery = FactoryRecovery(self.native, self.factory, clock=self.clock)

    def prepare(self, role="build", *, operation_id=None, binding=None):
        identity = operation_id or str(uuid4())
        request = {
            "operation_id": identity,
            "source_commit": "a" * 40,
            "prompt_digest": "b" * 64,
            "input_digest": "c" * 64,
        }
        self.factory["roles"][f"{role}:{self.factory['round']}"] = {"request": request}
        deadline = self.recovery.begin_role(
            role, identity, self.native.deadline(60), binding=binding
        )
        request["deadline"] = deadline
        manifest = {
            "run_id": self.native.run_id,
            "operation_id": identity,
            "deadline": deadline,
        }
        self.factory["roles"][f"{role}:{self.factory['round']}"]["session"] = {
            "manifest": manifest,
            "execution_identity": deepcopy(self.native.executor.identity),
        }
        self.native.executor.manifests[identity] = manifest
        self.native.executor.states[identity] = {
            "operation_id": identity,
            "state": "not-started",
            "execution_identity": deepcopy(self.native.executor.identity),
        }
        return identity

    def dispatch(self, identity):
        self.assertTrue(self.recovery.before_dispatch(identity))
        self.native.executor.states[identity] = {
            "operation_id": identity,
            "state": "running",
            "execution_identity": deepcopy(self.native.executor.identity),
        }

    def finish(self, identity, elapsed):
        started = self.clock()
        self.clock.now += elapsed
        status = self.native.executor.exited(identity, started=started)
        self.native.executor.states[identity] = status
        return self.recovery.observe_role(identity, status)

    def failure(self, reason="checks", *, verdict="changes"):
        native = self.native.native
        native.setdefault("validation", {"result_id": str(uuid4())})
        context, plan, candidate = (
            native["context"],
            native["plan"],
            native["candidate"],
        )
        payloads = {
            "checks": {
                "validation_id": native["validation"]["result_id"],
                "state": "failed",
            }
        }
        if reason == "reviews":
            payloads["checks"]["state"] = "passed"
            payloads["routing"] = {"required_reviewers": ["reviewer-code"]}
            payloads["reviewer-code"] = {"verdict": verdict}
        records = []
        for role, payload in payloads.items():
            document = {
                "run_id": self.native.run_id,
                "role": role,
                "round": self.factory["round"],
                "context_id": context["id"],
                "context_digest": context["digest"],
                "specification_id": plan["id"],
                "specification_digest": plan["digest"],
                "source_digest": candidate["source_digest"],
                "tree_sha": candidate["workspace"]["tree_sha"],
                "head_sha": candidate["workspace"]["commit_sha"],
                "base_sha": candidate["workspace"]["base_revision"],
                "configuration_digest": digest(self.native.snapshot),
                "payload": payload,
            }
            saved = {
                "id": str(uuid4()),
                "digest": digest(document),
                "document": document,
            }
            self.factory["artifacts"][f"{role}:{self.factory['round']}"] = saved
            records.append(saved)
        self.factory["repair_trigger"] = {
            "reason": reason,
            "round": self.factory["round"],
            "artifact_ids": [record["id"] for record in records],
            "source_digest": candidate["source_digest"],
            "specification_id": plan["id"],
            "specification_digest": plan["digest"],
        }
        return records

    def test_one_budget_charges_every_agent_role(self):
        for role in ("intake", "specification", "build", "reviewer-code", "decision"):
            identity = self.prepare(role)
            self.dispatch(identity)
            self.assertTrue(self.finish(identity, 3))
        self.assertEqual(self.recovery.budget.remaining("implementation"), 15)
        self.assertEqual(self.recovery.budget.data["total"], 15)

    def test_restart_preserves_role_time_and_frozen_deadline(self):
        identity = self.prepare()
        self.dispatch(identity)
        deadline = self.recovery.data["operations"][identity]["deadline"]
        self.clock.now += 8
        self.recovery.observe_role(identity, self.native.executor.status(identity))
        restored = Native(self.journal, self.clock, state=self.journal.load())
        recovery = FactoryRecovery(
            restored, restored.native["factory"], clock=self.clock
        )
        self.assertEqual(recovery.budget.remaining("implementation"), 22)
        self.assertEqual(recovery.data["operations"][identity]["deadline"], deadline)
        with self.assertRaisesRegex(ReconciliationRequired, "original role"):
            self.prepare("reviewer-code")

    def test_completed_receipt_replay_does_not_charge_twice(self):
        identity = self.prepare()
        self.dispatch(identity)
        self.finish(identity, 7)
        self.assertTrue(
            self.recovery.observe_role(identity, self.native.executor.status(identity))
        )
        self.assertEqual(self.recovery.budget.remaining("implementation"), 23)

    def test_success_looking_terminal_status_requires_descendant_receipt(self):
        identity = self.prepare()
        self.dispatch(identity)
        status = self.native.executor.exited(identity)
        status["descendants_terminated"] = False
        with self.assertRaises(ValueError):
            self.recovery.observe_role(identity, status)

    def test_unknown_result_fences_replacement_even_without_artifact(self):
        identity = self.prepare()
        self.dispatch(identity)
        with self.assertRaisesRegex(ReconciliationRequired, "unknown"):
            self.recovery.observe_role(
                identity,
                {
                    "operation_id": identity,
                    "state": "unknown",
                    "execution_identity": deepcopy(self.native.executor.identity),
                },
            )
        with self.assertRaisesRegex(ReconciliationRequired, "original role"):
            self.prepare("reviewer-code")

    def test_dispatch_is_saved_before_paid_launch_and_cannot_repeat_blindly(self):
        identity = self.prepare()
        self.assertTrue(self.recovery.before_dispatch(identity))
        saved = self.journal.load()["native"]["factory"]["recovery"]["operations"][
            identity
        ]
        self.assertEqual(saved["state"], "dispatching")
        with self.assertRaisesRegex(ReconciliationRequired, "original dispatch"):
            self.recovery.before_dispatch(identity)

    def test_protected_not_started_proof_can_retry_same_operation(self):
        identity = self.prepare()
        self.recovery.before_dispatch(identity)
        self.assertFalse(
            self.recovery.observe_role(identity, self.native.executor.status(identity))
        )
        self.assertTrue(self.recovery.before_dispatch(identity))
        self.assertEqual(len(self.recovery.data["operations"]), 1)

    def test_missing_original_record_cannot_authorize_another_dispatch(self):
        identity = self.prepare()
        self.recovery.before_dispatch(identity)
        self.native.executor.manifests[identity]["deadline"] = "changed"
        with self.assertRaisesRegex(ReconciliationRequired, "not-started proof"):
            self.recovery.observe_role(identity, self.native.executor.status(identity))

    def test_review_reuse_requires_identical_spec_and_tree(self):
        binding = {"specification_id": str(uuid4()), "source_digest": "a" * 64}
        identity = self.prepare("reviewer-code", binding=binding)
        self.dispatch(identity)
        self.finish(identity, 2)
        request = self.factory["roles"]["reviewer-code:1"]["request"]
        self.assertEqual(
            self.recovery.begin_role(
                "reviewer-code", identity, request["deadline"], binding=binding
            ),
            request["deadline"],
        )
        with self.assertRaisesRegex(ReconciliationRequired, "binding"):
            self.recovery.begin_role(
                "reviewer-code",
                identity,
                request["deadline"],
                binding={**binding, "source_digest": "b" * 64},
            )

    def configure_repairs(self, limit):
        self.native.snapshot["effective"] = {
            "factory": {"enabled": True, "max_repairs": limit}
        }
        self.factory.pop("recovery")
        self.recovery = FactoryRecovery(self.native, self.factory, clock=self.clock)

    def test_zero_configured_repairs_rejects_first_failed_check_repair(self):
        self.configure_repairs(0)
        self.failure()
        with self.assertRaisesRegex(BudgetExceeded, "shared repair"):
            self.recovery.repair("checks", "test failed")
        self.assertEqual(self.native.native["attempt"], 0)

    def test_one_configured_repair_is_shared_by_checks_and_reviews(self):
        self.configure_repairs(1)
        self.failure()
        self.recovery.repair("checks", "test failed")
        self.failure("reviews")
        with self.assertRaisesRegex(BudgetExceeded, "shared repair"):
            self.recovery.repair("reviews", "changes requested")
        self.assertEqual(self.native.native["attempt"], 1)

    def test_two_configured_repairs_allow_both_failure_sources(self):
        self.configure_repairs(2)
        self.failure()
        self.recovery.repair("checks", "test failed")
        self.failure("reviews")
        self.recovery.repair("reviews", "changes requested")
        self.assertEqual(self.native.native["attempt"], 2)

    def test_configured_limit_cannot_increase_on_restart(self):
        self.configure_repairs(0)
        self.native.snapshot["effective"]["factory"]["max_repairs"] = 1
        with self.assertRaisesRegex(ReconciliationRequired, "controls changed"):
            FactoryRecovery(self.native, self.factory, clock=self.clock)

    def test_repair_budget_is_shared_across_failure_sources(self):
        self.failure()
        self.recovery.repair("checks", "test failed")
        self.failure("reviews")
        self.recovery.repair("reviews", "changes requested")
        self.failure()
        with self.assertRaisesRegex(BudgetExceeded, "shared repair"):
            self.recovery.repair("checks", "another test failed")
        self.assertEqual(self.native.native["attempt"], 2)
        self.assertEqual(self.factory["round"], 3)

    def test_repairs_preserve_history_and_clear_only_current_candidate(self):
        self.factory["artifacts"]["reviewer-code:1"] = {"id": "historical"}
        self.factory["decision"] = {"verdict": "ready"}
        self.native.native["validation"] = {"state": "failed"}
        self.native.native["validation"]["result_id"] = str(uuid4())
        self.failure()
        self.recovery.repair("checks", "test failed")
        self.assertEqual(
            self.factory["artifacts"]["reviewer-code:1"]["id"], "historical"
        )
        self.assertNotIn("decision", self.factory)
        self.assertNotIn("validation", self.native.native)
        self.assertEqual(self.native.native["phase"], "factory")

    def test_unknown_cleanup_retains_attempt_and_fence(self):
        self.failure("reviews")
        identity = self.prepare()
        self.dispatch(identity)
        self.native.executor.uncertain = True
        with self.assertRaisesRegex(ReconciliationRequired, "termination"):
            self.recovery.repair("reviews", "changes")
        self.assertEqual(self.native.native["attempt"], 0)
        self.assertEqual(self.native.executor.stopped, [identity])

    def test_cancel_stops_running_role_before_repair_capacity(self):
        identity = self.prepare()
        self.dispatch(identity)
        self.clock.now += 5
        self.recovery.stop()
        self.assertEqual(self.native.executor.stopped, [identity])
        self.assertEqual(self.recovery.data["operations"][identity]["state"], "exited")
        self.assertEqual(self.recovery.budget.remaining("implementation"), 25)
        self.assertEqual(self.native.native["attempt"], 0)

    def test_aggregate_exhaustion_persists_before_failure(self):
        identity = self.prepare()
        self.dispatch(identity)
        with self.assertRaisesRegex(BudgetExceeded, "timeout"):
            self.finish(identity, 31)
        self.assertEqual(
            self.journal.load()["budgets"][self.native.run_id]["total"], 31
        )

    def test_paused_launch_checks_policy_without_resetting_clock(self):
        identity = self.prepare()
        self.native.allowed = False
        self.assertFalse(self.recovery.before_dispatch(identity))
        self.assertIsNone(self.recovery.data["operations"][identity]["dispatched_at"])
        self.clock.now += 31
        self.native.allowed = True
        with self.assertRaisesRegex(BudgetExceeded, "deadline"):
            self.recovery.before_dispatch(identity)

    def test_changed_credentials_cannot_select_a_replacement_account(self):
        self.native.native["context"]["document"]["account"]["credential_version"] = 2
        with self.assertRaisesRegex(ReconciliationRequired, "controls changed"):
            FactoryRecovery(self.native, self.factory, clock=self.clock)

    def test_worker_boot_change_cannot_rebind_original_operation(self):
        identity = self.prepare()
        session = self.factory["roles"]["build:1"]["session"]
        self.dispatch(identity)
        status = self.native.executor.status(identity)
        status["execution_identity"] = {
            **session["execution_identity"],
            "worker_boot": str(uuid4()),
        }
        with self.assertRaisesRegex(ReconciliationRequired, "worker instance or boot"):
            self.recovery.observe_role(identity, status)

    def test_frozen_repair_limit_cannot_increase_after_restart(self):
        self.native.policy.repair_limit = 1
        with self.assertRaisesRegex(ReconciliationRequired, "controls changed"):
            FactoryRecovery(self.native, self.factory, clock=self.clock)

    def test_changed_input_between_prepare_and_dispatch_is_rejected(self):
        identity = self.prepare()
        self.factory["roles"]["build:1"]["request"]["prompt_digest"] = "e" * 64
        with self.assertRaisesRegex(ReconciliationRequired, "input changed"):
            self.recovery.before_dispatch(identity)

    def test_unreceived_prepare_can_cancel_without_a_paid_dispatch(self):
        identity = self.prepare()
        self.factory["roles"]["build:1"].pop("session")
        self.native.executor.states.clear()
        self.native.resources.load = lambda run_id: {"operations": {}, "pending": {}}
        self.recovery.stop()
        record = self.recovery.data["operations"][identity]
        self.assertTrue(record["canceled_before_prepare"])
        self.assertEqual(record["state"], "exited")
        self.assertEqual(self.recovery.budget.data["total"], 0)

    def test_cancel_is_not_permission_to_start_another_build(self):
        with self.assertRaisesRegex(ReconciliationRequired, "failure evidence"):
            self.recovery.repair("cancel", "operator canceled")
        self.assertEqual(self.native.native["attempt"], 0)

    def test_prior_ci_repair_consumes_the_same_allowance(self):
        self.native.native["attempt"] = 1
        self.factory["round"] = 2
        recovery = FactoryRecovery(self.native, self.factory, clock=self.clock)
        self.failure("reviews")
        recovery.repair("reviews", "changes")
        self.failure()
        with self.assertRaisesRegex(BudgetExceeded, "shared repair"):
            recovery.repair("checks", "failed")
        self.assertEqual(self.native.native["attempt"], 2)

    def test_free_string_cannot_authorize_repair(self):
        with self.assertRaisesRegex(ReconciliationRequired, "failure evidence"):
            self.recovery.repair("checks", "test failed")
        self.assertEqual(self.native.native["attempt"], 0)

    def test_stale_failure_cannot_authorize_repair(self):
        self.failure()
        self.factory["repair_trigger"]["source_digest"] = "a" * 64
        with self.assertRaisesRegex(ReconciliationRequired, "stale"):
            self.recovery.repair("checks", "failed")

    def test_artifact_digest_change_cannot_authorize_repair(self):
        records = self.failure()
        records[0]["document"]["payload"]["state"] = "passed"
        with self.assertRaisesRegex(ReconciliationRequired, "acknowledgement"):
            self.recovery.repair("checks", "failed")

    def test_blocked_review_cannot_authorize_repair(self):
        self.failure("reviews", verdict="blocked")
        with self.assertRaisesRegex(ReconciliationRequired, "requesting changes"):
            self.recovery.repair("reviews", "blocked")

    def test_uncertain_repair_response_replays_without_extra_attempt(self):
        self.failure()
        self.recovery.repair("checks", "failed")
        restored = Native(self.journal, self.clock, state=self.journal.load())
        recovery = FactoryRecovery(
            restored, restored.native["factory"], clock=self.clock
        )
        self.assertEqual(recovery.repair("checks", "failed"), "continue")
        self.assertEqual(restored.native["attempt"], 1)
        self.assertEqual(len(recovery.data["repairs"]), 1)

    def test_restart_reconciles_interrupted_elapsed_accounting(self):
        identity = self.prepare()
        self.dispatch(identity)
        self.recovery.data["operations"][identity]["elapsed"] = 9
        self.native.save()
        restored = Native(self.journal, self.clock, state=self.journal.load())
        recovery = FactoryRecovery(
            restored, restored.native["factory"], clock=self.clock
        )
        self.assertEqual(recovery.budget.remaining("implementation"), 21)

    def test_changed_artifact_tree_cannot_authorize_repair(self):
        records = self.failure()
        records[0]["document"]["tree_sha"] = "a" * 40
        records[0]["digest"] = digest(records[0]["document"])
        with self.assertRaisesRegex(ReconciliationRequired, "another candidate"):
            self.recovery.repair("checks", "failed")

    def test_real_check_loop_supplies_failure_proof_to_shared_budget(self):
        from vibearr_runner.factory_reviews import FactoryReviewLoop

        self.failure()
        self.factory.pop("repair_trigger")
        candidate = self.native.native["candidate"]
        factory = SimpleNamespace(host=self.native, state=self.factory)
        loop = FactoryReviewLoop(factory)
        loop.source = lambda: SimpleNamespace(
            digest=candidate["source_digest"],
            tree_sha=candidate["workspace"]["tree_sha"],
            commit_sha=candidate["workspace"]["commit_sha"],
        )
        self.assertEqual(loop.repair("checks", "actual test failure"), "continue")
        self.assertEqual(self.native.native["attempt"], 1)
        self.assertEqual(self.factory["round"], 2)
        repair = self.recovery.data["repairs"][0]
        self.assertEqual(repair["trigger"]["round"], 1)
        self.assertEqual(len(repair["evidence"]["artifacts"]), 1)

    def test_native_cleanup_charges_role_even_after_budget_exhaustion(self):
        from vibearr_runner.native_lifecycle import NativeLifecycle

        identity = self.prepare()
        self.dispatch(identity)
        self.clock.now += 35
        NativeLifecycle.stop_resources(self.native)
        self.assertEqual(self.native.executor.stopped, [identity])
        self.assertEqual(self.recovery.budget.data["total"], 35)
        self.assertEqual(self.recovery.data["operations"][identity]["state"], "exited")

    def test_interruption_before_terminal_accounting_keeps_original_operation_pending(
        self,
    ):
        identity = self.prepare()
        self.dispatch(identity)
        self.clock.now += 4
        status = self.native.executor.exited(identity)
        with patch.object(
            self.recovery, "_charge", side_effect=RuntimeError("interrupted")
        ):
            with self.assertRaisesRegex(RuntimeError, "interrupted"):
                self.recovery.observe_role(identity, status)
        saved = self.journal.load()["native"]["factory"]["recovery"]["operations"][
            identity
        ]
        self.assertEqual(saved["state"], "dispatching")
        self.assertEqual(saved["elapsed"], 0)

    def test_claimed_run_base_or_head_change_invalidates_failure_evidence(self):
        self.failure()
        workspace = self.native.native["candidate"]["workspace"]
        original = deepcopy(workspace)
        for field in ("base_revision", "commit_sha"):
            workspace.update(original)
            workspace[field] = "a" * 40
            with (
                self.subTest(field=field),
                self.assertRaisesRegex(ReconciliationRequired, "another candidate"),
            ):
                self.recovery.repair("checks", "recheck before publication")
        self.assertEqual(self.native.native["attempt"], 0)
        self.assertEqual(self.factory["round"], 1)
        self.assertEqual(self.recovery.data["repairs"], [])
