from contextlib import redirect_stdout
from copy import deepcopy
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from uuid import UUID

from vibearr_runner import factory_acceptance as factory
from vibearr_runner.native_contract import digest
from vibearr_runner.factory_routing import default_configuration, route


def identity(number):
    return str(UUID(int=number))


def scenario():
    return {
        "schema_version": 1,
        "project_id": 7,
        "repository_id": 9,
        "repository": "owner/repo",
        "base_branch": "main",
        "provider": "codex",
        "agent_version": "codex-cli test-version",
        "connection_id": identity(90),
        "configuration_revision": "c" * 64,
        "source_revision": "a" * 40,
        "repair_limit": 2,
        "tasks": [
            {
                "issue_number": 10,
                "body_digest": digest("first"),
                "acceptance_criteria": ["Add a tested behavior"],
                "surfaces": [],
            },
            {
                "issue_number": 11,
                "body_digest": digest("Depends on: #10"),
                "acceptance_criteria": ["Extend the first behavior"],
                "surfaces": [],
            },
        ],
        "required_checks": [
            {
                "name": "Validate",
                "app_id": 12,
                "workflow_id": 13,
                "workflow_path": ".github/workflows/test.yml",
                "workflow_digest": "a" * 64,
            }
        ],
    }


def worker_identity():
    return {
        "runtime": factory.RUNTIME,
        "protocol_version": 2,
        "instance_id": identity(91),
        "worker_boot": identity(92),
        "binding_digest": "b" * 64,
    }


def chain(allowance=100):
    selected = scenario()
    configuration = {**default_configuration(), "enabled": True}
    snapshot = {
        "global": {"implementation_timeout_seconds": allowance},
        "effective": {"factory": configuration},
        "execution_policy": {"delivery_mode": "pull_request", "repair_limit": 2},
    }
    run = {
        "id": identity(1),
        "configuration_revision": selected["configuration_revision"],
        "configuration_snapshot": snapshot,
        "created_at": "2026-11-22T00:00:00+00:00",
        "finished_at": "2026-11-22T00:02:00+00:00",
        "context_reference": identity(2),
        "plan_reference": identity(3),
    }
    workspace = {
        "repository_id": 9,
        "repository": "owner/repo",
        "base_branch": "main",
        "base_revision": "a" * 40,
        "tree_sha": "b" * 40,
        "commit_sha": "c" * 40,
        "branch": "feature/first",
    }
    validation_id = identity(99)
    validation = {
        "state": "passed",
        "tree_sha": workspace["tree_sha"],
        "source_digest": "1" * 64,
        "run_id": run["id"],
    }
    native = [
        {
            "id": 99,
            "operation_id": validation_id,
            "kind": "native_validation",
            "data": validation,
        }
    ]
    entries = []
    roles = [
        "intake",
        "specification",
        "build",
        "checks",
        "routing",
        "reviewer-code",
        "decision",
    ]
    payloads = {
        "intake": {
            "verdict": "accept",
            "scope": "Add behavior",
            "criteria": ["A"],
            "rationale": "Useful",
            "questions": [],
            "risk": "low",
            "surfaces": [],
        },
        "specification": {
            "scope": "Add behavior",
            "criteria": ["A"],
            "exclusions": [],
            "verification": ["Run tests"],
        },
        "build": {"summary": "Implemented", "criteria": ["criterion-1"]},
        "checks": {"validation_id": validation_id, "state": "passed"},
        "reviewer-code": {
            "verdict": "pass",
            "findings": [],
            "coverage": ["criterion-1"],
        },
        "decision": {
            "verdict": "ready",
            "rationale": "Ready for a human",
            "artifact_ids": [identity(n) for n in (13, 14, 15)],
        },
    }
    paths = ["work_queue/models.py"]
    payloads["routing"] = route(configuration, payloads["intake"], paths)
    plan = {
        "id": identity(3),
        "context_id": identity(2),
        "document": {
            "factory_specification_id": identity(11),
            "factory_specification_digest": "",
        },
    }
    stopped = []
    for index, role in enumerate(roles):
        document = {
            "schema_version": 1,
            "run_id": identity(1),
            "operation_id": identity(100 + index),
            "role": role,
            "round": 1,
            "context_id": identity(2),
            "context_digest": "e" * 64,
            "specification_id": None if index < 2 else identity(3),
            "specification_digest": "" if index < 2 else "f" * 64,
            "source_digest": "1" * 64,
            "tree_sha": workspace["tree_sha"],
            "base_sha": workspace["base_revision"],
            "head_sha": workspace["commit_sha"],
            "configuration_digest": digest(snapshot),
            "payload": payloads[role],
        }
        if role not in {"routing", "checks"}:
            execution = {
                "intent_id": identity(200 + index),
                "result_id": identity(300 + index),
                "adapter": "codex",
                "model": "selected-model",
                "agent_version": selected["agent_version"],
                "session_id": f"session-{index}",
                "operation_id": identity(400 + index),
                "execution_identity": worker_identity(),
                "input_source_digest": "2" * 64,
                "input_digest": "3" * 64,
                "prompt_digest": "4" * 64,
                "started_at": 1795305601.0 + index * 2,
                "ended_at": 1795305602.0 + index * 2,
            }
            document["execution"] = execution
            native.extend(
                [
                    {
                        "id": 200 + index,
                        "operation_id": execution["intent_id"],
                        "kind": "native_intent",
                        "data": {
                            "kind": "agent",
                            "attempt": 0,
                            "context_id": document["context_id"],
                            "context_digest": document["context_digest"],
                            "data": {
                                "factory": {
                                    **{
                                        key: document[key]
                                        for key in (
                                            "role",
                                            "round",
                                            "specification_id",
                                            "specification_digest",
                                        )
                                    },
                                    "prompt_digest": execution["prompt_digest"],
                                    "input_digest": execution["input_digest"],
                                },
                                "session": {
                                    "adapter": execution["adapter"],
                                    "agent_version": execution["agent_version"],
                                    "manifest": {
                                        "operation_id": execution["operation_id"],
                                        "source_digest": execution[
                                            "input_source_digest"
                                        ],
                                        "context_digest": execution["input_digest"],
                                    },
                                    "account": {
                                        "credential_id": selected["connection_id"],
                                        "model": execution["model"],
                                    },
                                },
                            },
                        },
                    },
                    {
                        "id": 300 + index,
                        "operation_id": execution["result_id"],
                        "kind": "native_agent",
                        "data": {
                            "successful": True,
                            "session_id": execution["session_id"],
                            "agent_version": execution["agent_version"],
                            "intent_id": execution["intent_id"],
                            "receipt": {
                                "operation_id": execution["operation_id"],
                                "execution_identity": worker_identity(),
                                "started_at": execution["started_at"],
                                "ended_at": execution["ended_at"],
                            },
                        },
                    },
                ]
            )
            stopped.append(deepcopy(native[-1]["data"]["receipt"]))
        if role == "specification":
            plan["document"]["factory_specification_digest"] = digest(document)
            plan["digest"] = digest(plan["document"])
        if index >= 2:
            document["specification_digest"] = plan["digest"]
            if "execution" in document:
                native[-2]["data"]["data"]["factory"]["specification_digest"] = plan[
                    "digest"
                ]
        entries.append(
            {
                "id": identity(10 + index),
                "sequence": index + 1,
                "role": role,
                "round": 1,
                "operation_id": document["operation_id"],
                "context_id": document["context_id"],
                "specification_id": document["specification_id"],
                "document": document,
                "digest": digest(document),
            }
        )
    native.append(
        {
            "id": 500,
            "operation_id": identity(500),
            "kind": "native_termination",
            "data": {"operations": stopped},
        }
    )
    collected = {
        "run_id": identity(1),
        "finished_at": run["finished_at"],
        "claimed_at": run["created_at"],
        "delivery": {
            "workspace": workspace,
            "runtime": {"image_digest": "sha256:" + "5" * 64},
            "delivery": {"pull_request": {"number": 20}},
        },
        "validation": validation,
        "changed_files": paths,
        "_factory_documents": {
            "contexts": {identity(2): {"id": identity(2), "digest": "e" * 64}},
            "plans": {identity(3): plan},
        },
    }
    native.append(
        {
            "id": 501,
            "operation_id": identity(501),
            "kind": "native_workspace",
            "data": {
                "workspace": workspace,
                "context_id": identity(2),
                "context_digest": "e" * 64,
                "source_digest": "1" * 64,
                "factory_paths": paths,
            },
        }
    )
    return selected, run, collected, entries, native


def stack():
    from vibearr_runner.test_compact_evidence import valid_evidence

    return valid_evidence()


class FactoryStateTests(unittest.TestCase):
    def test_prepare_resumes_the_same_operation(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "acceptance"
            first = factory.prepare(directory, scenario())
            second = factory.prepare(directory, scenario())
            self.assertEqual(first["operation_id"], second["operation_id"])
            self.assertEqual(
                (directory / "factory-acceptance.json").stat().st_mode & 0o077, 0
            )

    def test_resume_cannot_relabel_fixture_evidence_as_live(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "acceptance"
            factory.prepare(directory, scenario())
            with self.assertRaisesRegex(ValueError, "original scenario"):
                factory.prepare(directory, scenario(), evidence_kind="live")

    def test_unsupported_scenario_version_is_rejected(self):
        value = scenario()
        value["schema_version"] = 99
        with self.assertRaisesRegex(ValueError, "Unsupported"):
            factory.scenario_document(value)

    def test_read_only_queue_never_dispatches_a_mutation(self):
        client = Mock()
        queue = factory.ReadOnlyQueue(client)
        with self.assertRaisesRegex(ValueError, "only reads"):
            queue.request("POST", "/api/v1/runner/merge/", {})
        client.request.assert_not_called()


class FactoryChainTests(unittest.TestCase):
    def setUp(self):
        self.scenario, self.run, self.collected, self.entries, self.native = chain()

    def check(self):
        return factory.check_factory_chain(
            self.entries,
            self.native,
            self.run,
            self.collected,
            self.scenario,
            self.collected["_factory_documents"],
        )

    def test_complete_current_round_references_every_required_role(self):
        result = self.check()
        self.assertEqual(result["required_reviewers"], ["reviewer-code"])
        self.assertEqual(result["rounds"], 1)

    def test_missing_reviewer_does_not_become_a_pass(self):
        self.entries = [
            entry for entry in self.entries if entry["role"] != "reviewer-code"
        ]
        with self.assertRaises(factory.DeliveryError):
            self.check()

    def test_review_for_another_tree_does_not_qualify_delivery(self):
        self.entries[-2]["document"]["tree_sha"] = "d" * 40
        with self.assertRaises(factory.DeliveryError):
            self.check()

    def test_reviewer_requested_changes_cannot_be_waived_by_decision(self):
        self.entries[-2]["document"]["payload"]["verdict"] = "changes"
        with self.assertRaises(factory.DeliveryError):
            self.check()

    def test_decision_must_reference_all_prerequisites(self):
        self.entries[-1]["document"]["payload"]["artifact_ids"] = [identity(12)]
        with self.assertRaises(factory.DeliveryError):
            self.check()

    def test_rounds_cannot_reset_the_shared_repair_budget(self):
        for entry in self.entries:
            entry["document"]["round"] = 4
        with self.assertRaisesRegex(ValueError, "repair budget"):
            self.check()

    def test_wrong_selected_connection_is_rejected(self):
        self.native[1]["data"]["data"]["session"]["account"]["credential_id"] = (
            identity(900)
        )
        with self.assertRaisesRegex(ValueError, "another selected connection"):
            self.check()

    def test_four_field_historical_dispatch_cannot_qualify_new_factory_work(self):
        descriptor = self.native[1]["data"]["data"]["factory"]
        del descriptor["specification_id"]
        del descriptor["specification_digest"]
        with self.assertRaisesRegex(ValueError, "six-field dispatch"):
            self.check()

    def test_dispatch_cannot_substitute_a_later_specification(self):
        descriptor = self.native[5]["data"]["data"]["factory"]
        descriptor["specification_id"] = identity(999)
        with self.assertRaisesRegex(ValueError, "six-field dispatch"):
            self.check()

    def test_role_cannot_underreport_actual_model_time(self):
        execution = self.entries[0]["document"]["execution"]
        execution["ended_at"] = execution["started_at"]
        self.entries[0]["digest"] = digest(self.entries[0]["document"])
        with self.assertRaisesRegex(ValueError, "genuine native result"):
            self.check()

    def test_unknown_worker_termination_cannot_release_acceptance(self):
        next(entry for entry in self.native if entry["kind"] == "native_termination")[
            "data"
        ]["operations"] = []
        with self.assertRaisesRegex(ValueError, "confirmed termination"):
            self.check()

    def test_role_time_cannot_reset_between_model_sessions(self):
        self.scenario, self.run, self.collected, self.entries, self.native = chain(
            allowance=1
        )
        with self.assertRaisesRegex(ValueError, "Aggregate role time"):
            self.check()

    def test_prior_integrated_runtime_does_not_qualify_compact_execution(self):
        self.entries[0]["document"]["execution"]["execution_identity"][
            "runtime"
        ] = "integrated-runner-v1"
        self.entries[0]["digest"] = digest(self.entries[0]["document"])
        self.native[2]["data"]["receipt"]["execution_identity"][
            "runtime"
        ] = "integrated-runner-v1"
        with self.assertRaisesRegex(ValueError, "Earlier integrated"):
            self.check()

    def test_automatic_merge_intent_invalidates_human_only_acceptance(self):
        self.native.append(
            {
                "id": 999,
                "operation_id": identity(999),
                "kind": "native_intent",
                "data": {"kind": "merge"},
            }
        )
        with self.assertRaisesRegex(ValueError, "automatic merge"):
            self.check()


class ArtifactReadTests(unittest.TestCase):
    def test_summary_pages_are_followed_by_actual_detail_reads(self):
        _, _, _, entries, _ = chain()
        artifact = entries[0]
        summary = {key: value for key, value in artifact.items() if key != "document"}
        calls = []

        def request(method, path, **_kwargs):
            calls.append((method, path))
            if path.endswith(artifact["id"] + "/"):
                return {"artifact": artifact}
            return {"artifacts": [summary], "has_more": False, "next_after": None}

        result = factory.factory_artifacts(
            SimpleNamespace(request=request), identity(1)
        )
        self.assertEqual(result, [artifact])
        self.assertEqual([method for method, _ in calls], ["GET", "GET"])

    def test_changed_detail_digest_is_rejected(self):
        _, _, _, entries, _ = chain()
        artifact = entries[0]
        summary = {key: value for key, value in artifact.items() if key != "document"}
        detail = deepcopy(artifact)
        detail["digest"] = "f" * 64
        queue = SimpleNamespace(
            request=Mock(
                side_effect=[
                    {"artifacts": [summary], "has_more": False},
                    {"artifact": detail},
                ]
            )
        )
        with self.assertRaisesRegex(ValueError, "immutable list identity"):
            factory.factory_artifacts(queue, identity(1))

    def test_an_empty_nonterminal_page_cannot_spin_forever(self):
        queue = SimpleNamespace(
            request=Mock(
                return_value={"artifacts": [], "has_more": True, "next_after": 0}
            )
        )
        with self.assertRaisesRegex(ValueError, "pagination is incomplete"):
            factory.factory_artifacts(queue, identity(1))
        queue.request.assert_called_once()

    def test_presentation_only_detail_never_becomes_immutable_evidence(self):
        _, _, _, entries, _ = chain()
        artifact = entries[0]
        summary = {key: value for key, value in artifact.items() if key != "document"}
        detail = {**artifact, "presentation_only": True}
        queue = SimpleNamespace(
            request=Mock(
                side_effect=[
                    {"artifacts": [summary], "has_more": False},
                    {"artifact": detail},
                ]
            )
        )
        with self.assertRaisesRegex(ValueError, "immutable list identity"):
            factory.factory_artifacts(queue, identity(1))


class ObserveTests(unittest.TestCase):
    def setUp(self):
        self.state = {"scenario": scenario(), "runs": [None, None]}
        self.response = {
            "run": {
                "id": identity(1),
                "configuration_revision": scenario()["configuration_revision"],
            },
            "item": {
                "project": {"id": 7},
                "issue_number": 10,
                "github_repository_id": 9,
            },
        }
        self.client = SimpleNamespace(request=Mock(return_value=self.response))

    def test_actual_run_is_pinned_using_only_read_requests(self):
        factory.observe(self.client, self.state, run_a=identity(1))
        self.assertEqual(self.state["runs"], [identity(1), None])
        self.client.request.assert_called_once_with(
            "GET", f"/api/v1/runner/runs/{identity(1)}/"
        )

    def test_observation_cannot_replace_the_original_run_after_restart(self):
        self.state["runs"][0] = identity(2)
        with self.assertRaisesRegex(ValueError, "original acceptance run"):
            factory.observe(self.client, self.state, run_a=identity(1))
        self.client.request.assert_not_called()

    def test_wrong_repository_does_not_change_checkpoint(self):
        self.response["item"]["github_repository_id"] = 999
        with self.assertRaisesRegex(ValueError, "frozen project"):
            factory.observe(self.client, self.state, run_a=identity(1))
        self.assertEqual(self.state["runs"], [None, None])

    def test_incomplete_observation_returns_finite_pending_report(self):
        self.state.update(
            operation_id=identity(99),
            scenario_digest=digest(scenario()),
            evidence_kind="fixture",
            snapshots={},
        )
        github = SimpleNamespace(read_only=True)
        report = factory.collect(
            self.client, github, self.state, stack_report={}, capabilities={}
        )
        self.assertEqual(report["stage"], "awaiting_delivery")
        self.assertFalse(report["qualified"])
        self.assertEqual(report["merge_writes"], 0)
        self.client.request.assert_not_called()


class GenerationTests(unittest.TestCase):
    def setUp(self):
        previous = {
            "account": {"credential_id": identity(90)},
            "repository": {"base_revision": "a" * 40},
        }
        marker = {
            "previous_context_id": identity(1),
            "previous_context_digest": digest(previous),
            "previous_workspace_id": identity(8),
            "attempt": 1,
            "old_base": "a" * 40,
            "new_base": "b" * 40,
            "reason": "base_update",
        }
        current = {
            "account": previous["account"],
            "repository": {"base_revision": "b" * 40},
            "factory_generation": marker,
        }
        self.contexts = {
            identity(1): {"document": previous, "digest": digest(previous)},
            identity(2): {"document": current, "digest": digest(current)},
        }
        self.run = {
            "context_reference": identity(2),
            "configuration_snapshot": {"execution_policy": {"repair_limit": 2}},
        }

    def test_linked_base_generation_preserves_frozen_authority(self):
        factory.verify_generations(self.contexts, self.run)

    def test_generation_cannot_change_the_selected_account(self):
        self.contexts[identity(2)]["document"]["account"] = {
            "credential_id": identity(99)
        }
        with self.assertRaisesRegex(ValueError, "frozen task, policy, account"):
            factory.verify_generations(self.contexts, self.run)

    def test_unlinked_generation_cannot_reset_shared_repair_history(self):
        self.contexts[identity(3)] = deepcopy(self.contexts[identity(1)])
        with self.assertRaisesRegex(ValueError, "unlinked context"):
            factory.verify_generations(self.contexts, self.run)


class HumanMergeTests(unittest.TestCase):
    def setUp(self):
        _, _, self.collected, _, _ = chain()
        self.pull = {
            "number": 20,
            "state": "closed",
            "merged": True,
            "auto_merge": None,
            "head": {"sha": "c" * 40},
            "merge_commit_sha": "d" * 40,
            "merged_at": "2026-11-22T00:03:00+00:00",
            "merged_by": {"id": 8, "login": "operator", "type": "User"},
        }

    def test_open_pr_waits_for_a_human_without_a_merge_write(self):
        self.pull.update(state="open", merged=False)
        github = SimpleNamespace(pull=Mock(return_value=self.pull), request=Mock())
        result = factory.observe_human_merge(github, self.collected)
        self.assertEqual(result["status"], "awaiting_human_merge")
        github.request.assert_not_called()

    def test_closed_unmerged_pr_does_not_unlock_dependencies(self):
        self.pull["merged"] = False
        with self.assertRaisesRegex(ValueError, "closed-unmerged"):
            factory.observe_human_merge(
                SimpleNamespace(pull=lambda *_: self.pull), self.collected
            )

    def test_bot_merge_cannot_substitute_for_the_required_human(self):
        self.pull["merged_by"]["type"] = "Bot"
        with self.assertRaisesRegex(ValueError, "human actor"):
            factory.observe_human_merge(
                SimpleNamespace(pull=lambda *_: self.pull), self.collected
            )

    def test_changed_head_needs_new_factory_reviews(self):
        self.pull["head"]["sha"] = "e" * 40
        with self.assertRaisesRegex(ValueError, "head changed"):
            factory.observe_human_merge(
                SimpleNamespace(pull=lambda *_: self.pull), self.collected
            )

    def test_stale_open_pull_is_rejected_before_waiting_for_a_human(self):
        self.pull.update(state="open", merged=False, head={"sha": "e" * 40})
        with self.assertRaisesRegex(ValueError, "head changed"):
            factory.observe_human_merge(
                SimpleNamespace(pull=lambda *_: self.pull), self.collected
            )

    def test_github_auto_merge_must_stay_disabled(self):
        self.pull["auto_merge"] = {"enabled_by": {"id": 8}}
        with self.assertRaisesRegex(ValueError, "auto-merge"):
            factory.observe_human_merge(
                SimpleNamespace(pull=lambda *_: self.pull), self.collected
            )


class QualificationTests(unittest.TestCase):
    def test_fixture_receipts_never_qualify_even_with_matching_worker_boot(self):
        report, capabilities = stack()
        with self.assertRaises(ValueError):
            factory.verify_stack(report, capabilities, report["source_revision"])

    def test_three_container_or_old_receipts_cannot_qualify(self):
        report, capabilities = stack()
        report["services"].remove("worker")
        with self.assertRaisesRegex(ValueError, "four-container"):
            factory.verify_stack(report, capabilities, report["source_revision"])

    def test_fixture_flags_cannot_be_relabelled_by_a_passed_boolean(self):
        report, capabilities = stack()
        report["fixtures"] = True
        with self.assertRaisesRegex(ValueError, "four-container"):
            factory.verify_stack(report, capabilities, report["source_revision"])

    def test_superseded_worker_boot_requires_new_qualification(self):
        report, capabilities = stack()
        capabilities["worker_boot"] = identity(999)
        with self.assertRaisesRegex(ValueError, "worker authority"):
            factory.verify_stack(report, capabilities, report["source_revision"])

    def test_missing_failure_matrix_keeps_release_unqualified(self):
        self.assertEqual(
            len(factory.verify_failures({}, None)), len(factory.FAILURE_CASES)
        )

    def test_terminated_operations_do_not_prove_named_factory_scenarios(self):
        receipt = {
            "operation_id": identity(30),
            "receipt_digest": "a" * 64,
            "execution_identity": worker_identity(),
        }
        status = {
            "state": "exited",
            "descendants_terminated": True,
            "receipt_digest": receipt["receipt_digest"],
            "execution_identity": worker_identity(),
        }
        worker = SimpleNamespace(status=lambda _operation: status)
        evidence = {
            name: {"passed": True, "fixtures": False, "receipts": [receipt]}
            for name in factory.FAILURE_CASES
        }
        missing = factory.verify_failures(evidence, worker)
        self.assertEqual(len(missing), len(factory.FAILURE_CASES))
        self.assertTrue(
            all("No trusted scenario producer" in value for value in missing)
        )

    def test_failure_receipt_must_match_actual_terminated_worker_operation(self):
        receipt = {
            "operation_id": identity(30),
            "receipt_digest": "a" * 64,
            "execution_identity": worker_identity(),
        }
        worker = SimpleNamespace(status=lambda _operation: {"state": "running"})
        with self.assertRaisesRegex(ValueError, "authenticated terminated"):
            factory.verify_failures(
                {
                    "cancellation": {
                        "passed": True,
                        "fixtures": False,
                        "receipts": [receipt],
                    }
                },
                worker,
            )

    def test_prepare_cli_never_constructs_provider_or_controller_clients(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "scenario.json"
            source.write_text(json.dumps(scenario()))
            source.chmod(0o600)
            with (
                patch.object(factory, "MergeGitHub") as github,
                redirect_stdout(io.StringIO()),
            ):
                code = factory.main(
                    [
                        "prepare",
                        "--state",
                        str(root / "state"),
                        "--scenario",
                        str(source),
                    ]
                )
            self.assertEqual(code, 0)
            github.assert_not_called()
