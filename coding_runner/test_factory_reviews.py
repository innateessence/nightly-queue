from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from vibearr_executor.contracts import Limits, Profile
from vibearr_runner.factory_reviews import (
    FactoryBrowserCapture,
    FactoryReviewError,
    FactoryReviewLoop,
    collect_factory_paths,
)
from vibearr_runner.factory_routing import default_configuration
from vibearr_runner.native_contract import digest
from vibearr_runner.native_validation import CandidateSource
from vibearr_runner.recovery import ReconciliationRequired
from vibearr_runner.workspaces import git


def reviewer(verdict="pass", *, coverage=None):
    findings = []
    if verdict != "pass":
        findings = [
            {
                "id": "finding-1",
                "summary": "Handle missing input.",
                "evidence": ["module.py:12"],
                "criterion_ids": ["criterion-1"],
            }
        ]
    return {
        "verdict": verdict,
        "findings": findings,
        "coverage": ["criterion-1", "criterion-2"] if coverage is None else coverage,
    }


class Factory:
    def __init__(self):
        self.source = CandidateSource("c" * 40, "d" * 40, b"candidate")
        config = default_configuration()
        config["enabled"] = True
        context = {"id": str(uuid4()), "digest": "a" * 64}
        plan = {
            "id": str(uuid4()),
            "digest": "b" * 64,
            "document": {"acceptance_criteria": ["First behavior", "Second behavior"]},
        }
        self.state = {"round": 1, "stage": "review", "roles": {}, "artifacts": {}}
        self.host = SimpleNamespace(
            native={
                "factory": self.state,
                "context": context,
                "plan": plan,
                "candidate": {
                    "source_digest": self.source.digest,
                    "factory_paths": ["module.py"],
                    "workspace": {
                        "commit_sha": self.source.commit_sha,
                        "base_revision": "e" * 40,
                    },
                },
                "validation": {
                    "result_id": str(uuid4()),
                    "evidence": {"checks": "passed"},
                },
            },
            run_id=str(uuid4()),
            snapshot={"effective": {"factory": config}},
            workspace=lambda: SimpleNamespace(checkout=Path("/candidate")),
            save=Mock(),
            executor=Mock(),
            resources=Mock(),
            checkpoint=Mock(return_value=True),
            journal=SimpleNamespace(directory=Path("/private")),
            status=Mock(),
            policy=SimpleNamespace(
                cpu_limit=1, memory_mb=512, pids_limit=64, workspace_mb=64
            ),
            stage_deadline=lambda field: "2030-01-01T00:00:00+00:00",
        )
        self.host.executor.logs.return_value = b"actual nonzero check diagnostic"
        self.host.native["validation"]["session"] = {
            "run_id": self.host.run_id,
            "source_digest": self.source.digest,
            "tree_sha": self.source.tree_sha,
        }
        self.state["artifacts"]["intake"] = {
            "document": {
                "payload": {
                    "verdict": "accept",
                    "risk": "low",
                    "surfaces": [],
                }
            }
        }
        self.state["artifacts"]["specification"] = {
            "document": {
                "payload": {
                    "criteria": ["First behavior", "Second behavior"],
                }
            }
        }
        self.calls = []
        self.results = {"reviewer-code": reviewer()}
        self.save_artifact(
            "build",
            {
                "summary": "Implemented both behaviors.",
                "criteria": ["criterion-1", "criterion-2"],
            },
            key="build:1",
        )
        self.save_artifact(
            "checks",
            {
                "validation_id": self.host.native["validation"]["result_id"],
                "state": "passed",
            },
            key="checks:1",
        )

    def save_artifact(self, role, payload, *, key, execution=None, source=None):
        source = source or self.source
        saved = {
            "id": str(uuid4()),
            "document": {
                "run_id": self.host.run_id,
                "role": role,
                "round": self.state["round"],
                "context_id": self.host.native["context"]["id"],
                "context_digest": self.host.native["context"]["digest"],
                "specification_id": self.host.native["plan"]["id"],
                "specification_digest": self.host.native["plan"]["digest"],
                "configuration_digest": digest(self.host.snapshot),
                "source_digest": source.digest,
                "tree_sha": source.tree_sha,
                "base_sha": self.host.native["candidate"]["workspace"]["base_revision"],
                "head_sha": source.commit_sha,
                "payload": deepcopy(payload),
            },
        }
        self.state["artifacts"][key] = saved
        return saved

    def specification_payload(self):
        return self.state["artifacts"]["specification"]["document"]["payload"]

    def run_role(self, role, *, key, payload=None, source=None):
        self.calls.append((role, key, payload))
        result = self.results.get(role)
        if callable(result):
            result = result(payload)
        if result is None or result.get("build_complete"):
            return result
        return self.save_artifact(role, result, key=key, source=source)


class FactoryReviewTests(unittest.TestCase):
    def setUp(self):
        self.factory = Factory()
        self.loop = FactoryReviewLoop(self.factory)
        self.loop.source = Mock(return_value=self.factory.source)
        self.paths = patch(
            "vibearr_runner.factory_reviews.git", return_value="module.py\x00"
        )
        self.paths.start()
        self.addCleanup(self.paths.stop)
        self.capabilities = patch(
            "vibearr_runner.factory_reviews.require_route_capabilities"
        )
        self.capabilities.start()
        self.addCleanup(self.capabilities.stop)

    def decision(self, payload):
        return {
            "verdict": "ready",
            "rationale": "The complete current chain passed.",
            "artifact_ids": [entry["id"] for entry in payload["required_artifacts"]],
        }

    def test_passing_checks_route_code_review_before_separate_decision(self):
        self.factory.results["decision"] = self.decision
        self.assertEqual(self.loop.step(), "continue")
        self.assertEqual(self.factory.calls[0][0], "reviewer-code")
        self.assertEqual(self.factory.state["stage"], "decision")
        self.assertEqual(self.loop.step(), "continue")
        self.assertEqual(
            [entry[0] for entry in self.factory.calls], ["reviewer-code", "decision"]
        )
        self.assertEqual(self.factory.host.native["phase"], "delivery")
        self.assertTrue(self.factory.state["current_decision"])

    def test_failed_gate_saves_actual_diagnostic_and_dispatches_no_reviewers(self):
        self.loop.repair = Mock(return_value="continue")
        self.assertEqual(
            self.loop.validated(
                {
                    "state": "failed",
                    "check_id": "tests",
                    "operation": {"operation_id": str(uuid4())},
                }
            ),
            "continue",
        )
        self.assertEqual(self.factory.calls, [])
        self.assertIn("actual nonzero", self.loop.repair.call_args.args[1])
        self.assertEqual(
            self.loop.artifact("checks")["document"]["payload"]["state"], "failed"
        )

    def test_missing_passing_gate_prevents_any_review(self):
        self.factory.state["artifacts"].pop("checks:1")
        with self.assertRaisesRegex(FactoryReviewError, "passing independent"):
            self.loop.step()
        self.assertEqual(self.factory.calls, [])

    def test_missing_trusted_workspace_paths_prevents_routing(self):
        self.factory.host.native["candidate"].pop("factory_paths")
        with self.assertRaisesRegex(FactoryReviewError, "changed-path inventory"):
            self.loop.step()
        self.assertEqual(self.factory.calls, [])

    def test_noncanonical_attested_paths_prevent_routing(self):
        self.factory.host.native["candidate"]["factory_paths"] = [
            "module.py",
            "module.py",
        ]
        with self.assertRaisesRegex(
            FactoryReviewError, "attestation must be canonical"
        ):
            self.loop.step()

    def test_missing_native_validation_evidence_prevents_review(self):
        self.factory.host.native["validation"].pop("evidence")
        with self.assertRaisesRegex(FactoryReviewError, "native validation evidence"):
            self.loop.step()

    def test_check_artifact_cannot_reference_another_protected_validation_receipt(self):
        self.factory.host.native["validation"]["result_id"] = str(uuid4())
        with self.assertRaisesRegex(
            FactoryReviewError, "current protected validation receipt"
        ):
            self.loop.step()
        self.assertEqual(self.factory.calls, [])

    def test_check_artifact_cannot_reuse_validation_from_another_source(self):
        self.factory.host.native["validation"]["session"]["source_digest"] = "f" * 64
        with self.assertRaisesRegex(
            FactoryReviewError, "current protected validation receipt"
        ):
            self.loop.step()
        self.assertEqual(self.factory.calls, [])

    def test_partial_review_barrier_does_not_advance_or_repeat_completed_role(self):
        self.factory.state["artifacts"]["intake"]["document"]["payload"]["surfaces"] = [
            "security"
        ]
        self.assertEqual(self.loop.step(), "running")
        self.assertEqual(self.factory.state["stage"], "review")
        self.factory.results["reviewer-security"] = reviewer()
        self.assertEqual(self.loop.step(), "continue")
        calls = [entry[0] for entry in self.factory.calls]
        self.assertEqual(calls.count("reviewer-code"), 1)
        self.assertNotIn("decision", calls)

    def test_changes_wait_for_all_reviewers_and_feed_all_findings_to_repair(self):
        self.factory.state["artifacts"]["intake"]["document"]["payload"]["surfaces"] = [
            "security"
        ]
        self.factory.results["reviewer-code"] = reviewer("changes")
        self.factory.results["reviewer-security"] = reviewer("changes")
        self.loop.repair = Mock(return_value="continue")
        self.assertEqual(self.loop.step(), "continue")
        self.assertEqual(
            [call[0] for call in self.factory.calls],
            ["reviewer-code", "reviewer-security"],
        )
        self.assertIn("reviewer-code finding-1", self.loop.repair.call_args.args[1])
        self.assertIn("reviewer-security finding-1", self.loop.repair.call_args.args[1])

    def test_blocked_review_is_terminal_without_starting_a_judge(self):
        self.factory.results["reviewer-code"] = reviewer("blocked")
        with self.assertRaisesRegex(FactoryReviewError, "Required review blocked"):
            self.loop.step()
        self.assertEqual([call[0] for call in self.factory.calls], ["reviewer-code"])

    def test_stale_review_source_is_rejected(self):
        saved = self.factory.save_artifact(
            "reviewer-code", reviewer(), key="reviewer-code:1"
        )
        saved["document"]["source_digest"] = "f" * 64
        with self.assertRaisesRegex(FactoryReviewError, "another candidate or round"):
            self.loop.step()

    def test_stale_review_round_is_rejected(self):
        saved = self.factory.save_artifact(
            "reviewer-code", reviewer(), key="reviewer-code:1"
        )
        saved["document"]["round"] = 2
        with self.assertRaisesRegex(FactoryReviewError, "another candidate or round"):
            self.loop.step()

    def test_passing_review_must_cover_all_specification_criteria(self):
        self.factory.results["reviewer-code"] = reviewer(coverage=["criterion-1"])
        with self.assertRaisesRegex(FactoryReviewError, "every criterion"):
            self.loop.step()

    def test_passing_review_cannot_waive_its_own_findings(self):
        result = reviewer("changes")
        result["verdict"] = "pass"
        self.factory.results["reviewer-code"] = result
        with self.assertRaisesRegex(FactoryReviewError, "without unresolved findings"):
            self.loop.step()

    def test_unknown_review_criterion_is_rejected(self):
        result = reviewer("changes")
        result["findings"][0]["criterion_ids"] = ["invented"]
        self.factory.results["reviewer-code"] = result
        with self.assertRaisesRegex(FactoryReviewError, "unknown criterion"):
            self.loop.step()

    def test_extra_review_result_fields_are_rejected(self):
        self.factory.results["reviewer-code"]["waive_checks"] = True
        with self.assertRaisesRegex(FactoryReviewError, "exact result schema"):
            self.loop.step()

    def test_extra_finding_fields_are_rejected(self):
        result = reviewer("changes")
        result["findings"][0]["waived"] = True
        self.factory.results["reviewer-code"] = result
        with self.assertRaisesRegex(FactoryReviewError, "exact result schema"):
            self.loop.step()

    def test_protected_path_change_blocks_without_reviewer_calls(self):
        self.factory.host.native["candidate"]["factory_paths"] = ["test.sh"]
        with self.assertRaisesRegex(FactoryReviewError, "protected validation"):
            self.loop.step()
        self.assertEqual(self.factory.calls, [])

    def test_ui_and_security_routes_require_real_browser_before_any_review(self):
        self.factory.state["artifacts"]["intake"]["document"]["payload"]["surfaces"] = [
            "ui",
            "security",
        ]
        with patch.object(FactoryBrowserCapture, "step", return_value=None):
            self.assertEqual(self.loop.step(), "running")
        self.assertEqual(self.factory.calls, [])

    def test_actual_browser_reference_is_only_sent_to_visual_roles(self):
        self.factory.state["artifacts"]["intake"]["document"]["payload"]["surfaces"] = [
            "ui",
            "security",
        ]
        for role in ("reviewer-security", "reviewer-ux", "reviewer-ui"):
            self.factory.results[role] = reviewer()
        capture = {
            "operation_id": str(uuid4()),
            "archive_digest": "9" * 64,
            "receipts": [{"sha256": "8" * 64}],
        }
        with patch.object(FactoryBrowserCapture, "step", return_value=capture):
            self.assertEqual(self.loop.step(), "continue")
        self.assertEqual(len(self.factory.calls), 4)
        for role, _key, payload in self.factory.calls:
            if role in {"reviewer-ui", "reviewer-ux"}:
                self.assertEqual(
                    set(payload["browser_capture"]), {"operation_id", "archive_digest"}
                )
            else:
                self.assertIsNone(payload["browser_capture"])

    def test_decision_cannot_skip_a_required_reviewer(self):
        self.loop.step()
        self.factory.state["artifacts"].pop("reviewer-code:1")
        with self.assertRaisesRegex(
            FactoryReviewError, "every current routed reviewer"
        ):
            self.loop.step()

    def test_decision_must_reference_complete_current_gate_and_reviews(self):
        self.loop.step()
        self.factory.results["decision"] = {
            "verdict": "ready",
            "rationale": "looks fine",
            "artifact_ids": [str(uuid4())],
        }
        with self.assertRaisesRegex(FactoryReviewError, "complete current gate"):
            self.loop.step()

    def test_unresolved_decision_never_delivers(self):
        self.loop.step()

        def unresolved(payload):
            result = self.decision(payload)
            result["verdict"] = "unresolved"
            return result

        self.factory.results["decision"] = unresolved
        with self.assertRaisesRegex(FactoryReviewError, "decision is unresolved"):
            self.loop.step()
        self.assertNotEqual(self.factory.host.native.get("phase"), "delivery")

    def test_new_round_cannot_reuse_old_checks_or_reviews(self):
        self.loop.step()
        self.factory.state.update(round=2, stage="review")
        with self.assertRaisesRegex(FactoryReviewError, "passing independent"):
            self.loop.step()

    def test_passing_checks_cannot_authorize_an_extra_builder_round(self):
        with self.assertRaisesRegex(FactoryReviewError, "actual failed check artifact"):
            self.loop.repair("checks", "Please retry anyway")

    def test_passing_reviews_cannot_authorize_an_extra_builder_round(self):
        self.loop.step()
        with self.assertRaisesRegex(
            FactoryReviewError, "concrete current review changes"
        ):
            self.loop.repair("reviews", "Please retry anyway")

    def test_builder_uses_spec_and_previous_diagnostics_then_imports_actual_export(
        self,
    ):
        self.factory.state["stage"] = "build"
        self.factory.host.native["repair_diagnostic"] = "Fix finding-1"
        self.factory.state["roles"]["build:1"] = {
            "request": {"operation_id": str(uuid4())}
        }
        self.factory.results["build"] = {"build_complete": True}
        self.assertEqual(self.loop.step(), "continue")
        self.assertEqual(self.factory.host.native["phase"], "import_candidate")
        self.assertEqual(self.factory.calls[0][2]["diagnostics"], "Fix finding-1")
        self.assertEqual(
            self.factory.calls[0][2]["specification"],
            self.factory.specification_payload(),
        )
        self.assertIsNot(
            self.factory.host.native["agent"], self.factory.state["roles"]["build:1"]
        )

    def test_imported_candidate_requires_a_successful_builder_receipt(self):
        self.factory.state["artifacts"].pop("build:1")
        with self.assertRaisesRegex(FactoryReviewError, "builder receipt"):
            self.loop.candidate_imported(self.factory.source)

    def builder_receipt(self):
        self.factory.state["artifacts"].pop("build:1")
        data = {
            "successful": True,
            "execution": {"intent_id": str(uuid4())},
            "intent_id": str(uuid4()),
            "export_digest": self.factory.source.digest,
            "role_result": {
                "summary": "Updated implementation.",
                "criteria": ["criterion-1", "criterion-2"],
            },
        }
        self.factory.state["roles"]["build:1"] = data
        self.factory.host.native["candidate"].update(
            agent_intent_id=data["intent_id"], export_digest=data["export_digest"]
        )
        return data

    def test_imported_candidate_cannot_use_another_builder_export_receipt(self):
        data = self.builder_receipt()
        data["export_digest"] = "f" * 64
        with self.assertRaisesRegex(
            FactoryReviewError, "differs from its builder receipt"
        ):
            self.loop.candidate_imported(self.factory.source)

    def test_imported_builder_must_map_every_saved_criterion(self):
        self.builder_receipt()["role_result"]["criteria"] = ["criterion-1"]
        with self.assertRaisesRegex(
            FactoryReviewError, "every saved specification criterion"
        ):
            self.loop.candidate_imported(self.factory.source)

    def test_imported_builder_cannot_return_malformed_criterion_mapping(self):
        self.builder_receipt()["role_result"]["criteria"] = [{"id": "criterion-1"}]
        with self.assertRaisesRegex(
            FactoryReviewError, "every saved specification criterion"
        ):
            self.loop.candidate_imported(self.factory.source)

    def test_oversized_review_diff_blocks_without_reviewer_dispatch(self):
        with patch(
            "vibearr_runner.factory_reviews.git", return_value="x" * (128 * 1024 + 1)
        ):
            with self.assertRaises(FactoryReviewError):
                self.loop.review()
        self.assertEqual(self.factory.calls, [])


class RealGitRoutingTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.checkout = Path(directory.name)
        git(["init", "-b", "main"], self.checkout)
        git(["config", "user.name", "Factory tests"], self.checkout)
        git(["config", "user.email", "factory@example.invalid"], self.checkout)
        git(["config", "diff.renames", "true"], self.checkout)
        self.factory = Factory()
        self.factory.host.workspace = lambda: SimpleNamespace(checkout=self.checkout)
        self.loop = FactoryReviewLoop(self.factory)

    def rename(self, original):
        path = self.checkout / original
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("preserved content\n")
        git(["add", "."], self.checkout)
        git(["commit", "-m", "base"], self.checkout)
        base = git(["rev-parse", "HEAD"], self.checkout)
        git(["mv", original, "ordinary.py"], self.checkout)
        git(["commit", "-m", "rename"], self.checkout)
        source = CandidateSource.from_git(
            self.checkout, git(["rev-parse", "HEAD"], self.checkout)
        )
        self.factory.source = source
        self.factory.host.native["candidate"] = {
            "source_digest": source.digest,
            "factory_paths": collect_factory_paths(
                self.checkout, base, source.commit_sha
            ),
            "workspace": {"base_revision": base, "commit_sha": source.commit_sha},
        }
        self.assertEqual(
            git(
                ["diff", "--name-status", "-M", base, source.commit_sha], self.checkout
            ).split()[0],
            "R100",
        )

    def test_security_rename_retains_required_security_review(self):
        self.rename("credentials/secret.py")
        self.assertEqual(
            self.factory.host.native["candidate"]["factory_paths"],
            ["credentials/secret.py", "ordinary.py"],
        )
        with patch("vibearr_runner.factory_reviews.require_route_capabilities"):
            routed = self.loop.routing()
        self.assertIn("reviewer-security", routed["required_reviewers"])

    def test_ui_rename_retains_required_visual_reviews(self):
        self.rename("templates/page.html")
        with patch("vibearr_runner.factory_reviews.require_route_capabilities"):
            routed = self.loop.routing()
        self.assertIn("reviewer-ux", routed["required_reviewers"])
        self.assertIn("reviewer-ui", routed["required_reviewers"])

    def test_protected_rename_blocks_routing(self):
        self.rename("test.sh")
        with self.assertRaisesRegex(FactoryReviewError, "protected validation"):
            self.loop.routing()
        self.assertNotIn("routing:1", self.factory.state["artifacts"])

    def test_review_diff_contains_both_sides_of_the_actual_rename(self):
        self.rename("credentials/secret.py")
        difference = self.loop.candidate_diff()
        self.assertIn("--- a/credentials/secret.py", difference["patch"])
        self.assertIn("+++ b/ordinary.py", difference["patch"])
        self.assertEqual(difference["head_sha"], self.factory.source.commit_sha)

    def test_collector_preserves_leading_whitespace_in_git_paths(self):
        self.rename(" leading.py")
        self.assertEqual(
            self.factory.host.native["candidate"]["factory_paths"],
            [" leading.py", "ordinary.py"],
        )


class BrowserCaptureTests(unittest.TestCase):
    def setUp(self):
        self.factory = Factory()
        self.host = self.factory.host
        self.profile = Profile(
            "browser-ui",
            1,
            "sha256:" + "a" * 64,
            "validation",
            ("python", "-m", "browser"),
            Limits(),
        )
        self.host.executor.capabilities.return_value = {
            "profiles": [
                {**self.profile.data(), "profile_digest": self.profile.digest},
            ]
        }
        self.host.executor.upload.side_effect = lambda kind, content: {
            "sha256": "f" * 64
        }
        self.capture = FactoryBrowserCapture(self.host, self.factory.state)
        self.identity = {
            "runtime": "compose-slot-v2",
            "protocol_version": 2,
            "instance_id": str(uuid4()),
            "worker_boot": str(uuid4()),
            "binding_digest": "d" * 64,
        }
        self.host.executor.prepare.return_value = {"execution_identity": self.identity}

    def test_missing_browser_profile_blocks(self):
        self.host.executor.capabilities.return_value = {"profiles": []}
        with self.assertRaisesRegex(FactoryReviewError, "unavailable"):
            self.capture.step(self.factory.source)

    def test_browser_operation_identity_is_journaled_before_dispatch(self):
        def status(operation):
            return {
                "operation_id": operation,
                "state": "not-started",
                "execution_identity": self.identity,
            }

        self.host.executor.status.side_effect = status
        self.assertIsNone(self.capture.step(self.factory.source))
        data = self.factory.state["browser"]["1"]
        self.assertEqual(data["manifest"]["source_digest"], self.factory.source.digest)
        self.host.executor.start.assert_called_once_with(data["operation_id"])
        self.host.resources.dispatch.assert_called_once_with(
            self.host.run_id, data["operation_id"]
        )

    def test_unknown_browser_operation_is_not_replaced(self):
        self.host.executor.status.side_effect = lambda operation: {
            "operation_id": operation,
            "state": "unknown",
        }
        with self.assertRaises(ReconciliationRequired):
            self.capture.step(self.factory.source)
        original = self.factory.state["browser"]["1"]["operation_id"]
        with self.assertRaises(ReconciliationRequired):
            self.capture.step(self.factory.source)
        self.assertEqual(self.factory.state["browser"]["1"]["operation_id"], original)
        self.host.executor.start.assert_not_called()

    def test_changed_candidate_cannot_reuse_browser_operation(self):
        self.host.executor.status.side_effect = lambda operation: {
            "operation_id": operation,
            "state": "running",
            "execution_identity": self.identity,
        }
        self.capture.step(self.factory.source)
        changed = CandidateSource("e" * 40, "d" * 40, b"new candidate")
        with self.assertRaisesRegex(FactoryReviewError, "different candidate"):
            self.capture.step(changed)

    def test_failed_browser_exit_blocks_review(self):
        self.host.executor.status.side_effect = lambda operation: {
            "operation_id": operation,
            "state": "exited",
            "descendants_terminated": True,
            "result": {"exit_code": 1},
            "execution_identity": self.identity,
        }
        with patch("vibearr_runner.factory_reviews.receipt_summary", return_value={}):
            with self.assertRaisesRegex(FactoryReviewError, "failed or did not stop"):
                self.capture.step(self.factory.source)

    def test_browser_profile_cannot_exceed_frozen_resource_policy(self):
        self.host.policy.memory_mb = 256
        with self.assertRaisesRegex(FactoryReviewError, "exceed the frozen"):
            self.capture.step(self.factory.source)
        self.host.executor.start.assert_not_called()

    def test_browser_operation_cannot_move_to_a_replacement_worker_boot(self):
        self.host.executor.status.side_effect = lambda operation: {
            "operation_id": operation,
            "state": "running",
            "execution_identity": self.identity,
        }
        self.capture.step(self.factory.source)
        replacement = {**self.identity, "worker_boot": str(uuid4())}
        self.host.executor.prepare.return_value = {"execution_identity": replacement}
        with self.assertRaisesRegex(ReconciliationRequired, "worker identity changed"):
            self.capture.step(self.factory.source)
        self.host.executor.start.assert_not_called()

    def completed(self):
        self.host.executor.status.side_effect = lambda operation: {
            "operation_id": operation,
            "state": "exited",
            "descendants_terminated": True,
            "result": {"exit_code": 0},
            "execution_identity": self.identity,
        }
        self.host.executor.browser_result.return_value = (
            {"screenshots": []},
            b"sealed capture",
        )
        return {
            "run_id": self.host.run_id,
            "source_digest": self.factory.source.digest,
            "export_digest": self.factory.source.digest,
            "execution_identity": self.identity,
        }

    def test_successful_browser_import_uses_actual_worker_receipt_and_candidate(self):
        receipt = self.completed()
        frames = [{"sha256": "8" * 64}]
        with (
            patch(
                "vibearr_runner.factory_reviews.receipt_summary", return_value=receipt
            ),
            patch(
                "vibearr_runner.browser_receipts.import_worker_capture",
                create=True,
                return_value=frames,
            ) as imported,
        ):
            captured = self.capture.step(self.factory.source)
        self.assertEqual(captured["receipts"], frames)
        self.assertEqual(
            imported.call_args.kwargs["worker_binding"]["execution_identity"],
            self.identity,
        )
        self.assertEqual(
            imported.call_args.kwargs["binding"]["source_digest"],
            self.factory.source.digest,
        )

    def test_mutated_browser_candidate_blocks_before_import(self):
        receipt = self.completed()
        receipt["export_digest"] = "f" * 64
        with patch(
            "vibearr_runner.factory_reviews.receipt_summary", return_value=receipt
        ):
            with self.assertRaisesRegex(FactoryReviewError, "changed its candidate"):
                self.capture.step(self.factory.source)

    def test_source_bound_browser_import_failure_blocks_reviews(self):
        receipt = self.completed()
        with (
            patch(
                "vibearr_runner.factory_reviews.receipt_summary", return_value=receipt
            ),
            patch(
                "vibearr_runner.browser_receipts.import_worker_capture",
                create=True,
                side_effect=ValueError("PNG differs"),
            ),
        ):
            with self.assertRaisesRegex(FactoryReviewError, "PNG differs"):
                self.capture.step(self.factory.source)


class NativeFactoryTransitionTests(unittest.TestCase):
    def setUp(self):
        from coding_runner.test_native_lifecycle import NativeLifecycleTests

        self.fixture = NativeLifecycleTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.factory_type = Mock()
        module = SimpleNamespace(FactoryLifecycle=self.factory_type)
        self.module_patch = patch.dict(
            sys.modules, {"vibearr_runner.factory_lifecycle": module}
        )
        self.module_patch.start()
        self.addCleanup(self.module_patch.stop)

    def enable_factory(self):
        snapshot = self.fixture.state["run"]["run"]["configuration_snapshot"]
        snapshot["effective"]["factory"] = {"enabled": True}

    def test_actual_native_validation_routes_to_factory_barrier_instead_of_delivery(
        self,
    ):
        self.fixture.advance("validation")
        self.enable_factory()
        callback = Mock(return_value="continue")
        with patch.object(FactoryReviewLoop, "validated", callback):
            for _ in range(30):
                self.fixture.engine().step(self.fixture.state)
                if callback.called:
                    break
        self.assertTrue(callback.called)
        self.assertEqual(callback.call_args.args[0]["state"], "passed")
        self.assertIn("result_id", self.fixture.state["native"]["validation"])
        self.assertNotEqual(self.fixture.state["native"]["phase"], "delivery")

    def test_actual_native_import_finalizes_builder_against_committed_candidate(self):
        self.fixture.advance("import_candidate")
        self.enable_factory()
        callback = Mock()
        with patch.object(FactoryReviewLoop, "candidate_imported", callback):
            self.assertEqual(self.fixture.engine().step(self.fixture.state), "continue")
        source = callback.call_args.args[0]
        self.assertEqual(
            source.digest, self.fixture.state["native"]["candidate"]["source_digest"]
        )
        self.assertEqual(
            source.commit_sha,
            self.fixture.state["native"]["candidate"]["workspace"]["commit_sha"],
        )


class NativeFactoryEngineTests(unittest.TestCase):
    def setUp(self):
        from coding_runner.test_factory_spec_sessions import (
            CRITERIA,
            FactorySpecSessionTests,
            RoleExecutor,
        )
        from coding_runner.test_native_lifecycle import Executor
        from vibearr_executor.artifacts import pack, result_manifest, unpack

        self.fixture = FactorySpecSessionTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        fixture = self.fixture
        criterion_ids = [entry["id"] for entry in CRITERIA]

        class BuildReviewExecutor(RoleExecutor):
            mutate_reviewer = False
            changes_once = False
            malformed_reviewer = False

            def start(self, operation):
                manifest = self.manifests[operation]
                if not manifest["profile_id"].startswith("factory-"):
                    return Executor.start(self, operation)
                role = manifest["profile_id"].removeprefix("factory-")
                if role.startswith("reviewer-"):
                    self.results[role] = reviewer(coverage=criterion_ids)
                    if self.malformed_reviewer:
                        self.results[role].pop("coverage")
                    if self.changes_once:
                        self.results[role] = reviewer("changes", coverage=criterion_ids)
                        self.results[role]["findings"][0]["criterion_ids"] = [
                            "answer-test"
                        ]
                        self.changes_once = False
                if role == "decision":
                    state = fixture.state["native"]["factory"]
                    number = state["round"]
                    routing = state["artifacts"][f"routing:{number}"]["document"][
                        "payload"
                    ]
                    roles = ["checks", "routing", *routing["required_reviewers"]]
                    self.results[role] = {
                        "verdict": "ready",
                        "rationale": "Every required gate passed.",
                        "artifact_ids": [
                            state["artifacts"][f"{name}:{number}"]["id"]
                            for name in roles
                        ],
                    }
                self.mutate_source = self.mutate_reviewer and role.startswith(
                    "reviewer-"
                )
                result = super().start(operation)
                if role == "build":
                    source = self.artifacts[manifest["source_digest"]]
                    files = unpack(source)
                    files["answer.py"] = (
                        0o644,
                        f"answer = {len(self.started)}\n".encode(),
                    )
                    self.outputs[operation] = pack(files)
                    self.states[operation]["result"]["artifact"] = result_manifest(
                        source, self.outputs[operation]
                    )
                    result = self.status(operation)
                return result

        fixture.executor = BuildReviewExecutor(fixture.profile)
        fixture.executor.results["build"] = {
            "summary": "Updated answer behavior.",
            "criteria": criterion_ids,
        }
        config = fixture.state["run"]["run"]["configuration_snapshot"]["effective"][
            "factory"
        ]
        fixture.state["run"]["run"]["configuration_snapshot"]["effective"][
            "factory"
        ] = {
            **default_configuration(),
            **config,
        }

    def advance(self, predicate):
        for _ in range(100):
            self.fixture.step()
            if predicate():
                return
            if self.fixture.queue.finishes:
                self.fail(str(self.fixture.queue.finishes[-1]))
        self.fail("Factory native engine did not reach its expected stage.")

    def roles(self):
        return [
            self.fixture.executor.manifests[operation]["profile_id"].removeprefix(
                "factory-"
            )
            for operation in self.fixture.executor.started
            if self.fixture.executor.manifests[operation]["profile_id"].startswith(
                "factory-"
            )
        ]

    def test_actual_role_engine_imports_checks_reviews_then_decides(self):
        self.advance(lambda: self.fixture.state["native"].get("phase") == "delivery")
        self.assertEqual(
            self.roles(),
            ["intake", "specification", "build", "reviewer-code", "decision"],
        )
        records = {
            entry["document"]["role"]: entry["document"]
            for entry in self.fixture.queue.factory_artifacts
        }
        source = self.fixture.state["native"]["candidate"]["source_digest"]
        for role in ("build", "checks", "routing", "reviewer-code", "decision"):
            self.assertEqual(records[role]["source_digest"], source)
            self.assertEqual(records[role]["round"], 1)
        self.assertEqual(records["checks"]["payload"]["state"], "passed")
        self.assertEqual(
            self.fixture.state["native"]["candidate"]["factory_paths"], ["answer.py"]
        )
        workspace = next(
            entry
            for entry in self.fixture.queue.artifacts
            if entry["kind"] == "native_workspace"
        )
        self.assertEqual(workspace["data"]["factory_paths"], ["answer.py"])
        self.assertEqual(self.fixture.queue.finishes, [])

    def test_actual_failed_checks_dispatch_no_reviewer_and_start_new_build_round(self):
        self.fixture.executor.validation_failures = 1
        self.advance(
            lambda: self.fixture.state["native"].get("factory", {}).get("round") == 2
        )
        self.assertEqual(self.roles(), ["intake", "specification", "build"])
        self.assertEqual(self.fixture.state["native"]["factory"]["stage"], "build")
        self.assertIn("failed", self.fixture.state["native"]["repair_diagnostic"])

    def test_actual_security_route_uses_distinct_read_only_review_sessions(self):
        self.fixture.executor.results["intake"]["surfaces"] = ["security"]
        self.advance(lambda: self.fixture.state["native"].get("phase") == "delivery")
        self.assertEqual(
            self.roles(),
            [
                "intake",
                "specification",
                "build",
                "reviewer-code",
                "reviewer-security",
                "decision",
            ],
        )
        state = self.fixture.state["native"]["factory"]
        code = state["artifacts"]["reviewer-code:1"]["document"]
        security = state["artifacts"]["reviewer-security:1"]["document"]
        self.assertNotEqual(
            code["execution"]["session_id"], security["execution"]["session_id"]
        )
        self.assertNotEqual(
            code["execution"]["operation_id"], security["execution"]["operation_id"]
        )
        self.assertEqual(code["source_digest"], security["source_digest"])
        self.assertEqual(code["specification_digest"], security["specification_digest"])

    def test_actual_malformed_reviewer_result_blocks_without_decision(self):
        self.fixture.executor.malformed_reviewer = True
        for _ in range(100):
            self.fixture.step()
            if self.fixture.queue.finishes:
                break
        self.assertTrue(self.fixture.queue.finishes)
        self.assertNotIn("decision", self.roles())
        self.assertNotIn(
            "reviewer-code:1", self.fixture.state["native"]["factory"]["artifacts"]
        )

    def test_actual_review_changes_repeat_import_gate_and_every_review(self):
        self.fixture.executor.changes_once = True
        self.advance(lambda: self.fixture.state["native"].get("phase") == "delivery")
        self.assertEqual(
            self.roles(),
            [
                "intake",
                "specification",
                "build",
                "reviewer-code",
                "build",
                "reviewer-code",
                "decision",
            ],
        )
        state = self.fixture.state["native"]["factory"]
        self.assertEqual(state["round"], 2)
        self.assertNotEqual(
            state["artifacts"]["checks:1"]["document"]["source_digest"],
            state["artifacts"]["checks:2"]["document"]["source_digest"],
        )
        self.assertIn("finding-1", self.fixture.state["native"]["repair_diagnostic"])

    def test_actual_reviewer_source_modification_never_produces_a_pass_or_decision(
        self,
    ):
        self.fixture.executor.mutate_reviewer = True
        for _ in range(100):
            self.fixture.step()
            if self.fixture.queue.finishes:
                break
        self.assertTrue(self.fixture.queue.finishes)
        self.assertNotIn("decision", self.roles())
        roles = [
            entry["document"]["role"] for entry in self.fixture.queue.factory_artifacts
        ]
        self.assertNotIn("reviewer-code", roles)


if __name__ == "__main__":
    unittest.main()
