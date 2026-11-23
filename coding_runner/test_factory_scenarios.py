from copy import deepcopy
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import UUID, uuid5

from coding_runner.test_factory_acceptance import chain, identity, worker_identity
from vibearr_executor.validation_profile import CHECK_IDS, ValidationProfile
from vibearr_runner import factory_scenarios as scenarios
from vibearr_runner.factory_routing import AGENT_ROLES, route
from vibearr_runner.native_contract import (
    digest,
    profile_reference,
    receipt_summary,
    termination_record,
    validation_evidence,
    workspace_record,
)
from coding_runner.test_native_evidence import receipt as delivery_receipt
from vibearr_runner.self_acceptance import timestamp


OPERATION = identity(90000)


class Queue:
    def __init__(self, fixture):
        self.fixture = fixture
        self.calls = []

    def reconcile(self, run_id):
        self.calls.append(("GET", run_id))
        if run_id != self.fixture.run["id"]:
            raise AssertionError("Unexpected run")
        return {"run": deepcopy(self.fixture.run), "item": deepcopy(self.fixture.item)}

    def request(self, method, path, payload=None, **kwargs):
        self.calls.append((method, path))
        if method != "GET" or payload is not None:
            raise AssertionError("Observer mutated the queue")
        fixture = self.fixture
        if "/contexts/" in path:
            return {"context": deepcopy(fixture.context)}
        if "/plans/" in path:
            return {"plan": deepcopy(fixture.plan)}
        if path.endswith("/evidence/"):
            after = kwargs.get("query", {}).get("after", 0)
            entries = [entry for entry in fixture.entries if entry["id"] > after][:10]
            return {
                "evidence": deepcopy(entries),
                "has_more": (
                    entries[-1]["id"] < fixture.entries[-1]["id"] if entries else False
                ),
                "next_after": entries[-1]["id"] if entries else after,
            }
        if path.endswith("/factory/artifacts/"):
            after = kwargs.get("query", {}).get("after", 0)
            entries = [
                entry for entry in fixture.artifacts if entry["sequence"] > after
            ][:10]
            return {
                "artifacts": deepcopy(entries),
                "has_more": (
                    entries[-1]["sequence"] < len(fixture.artifacts)
                    if entries
                    else False
                ),
                "next_after": entries[-1]["sequence"] if entries else after,
            }
        for entry in fixture.artifacts:
            if path.endswith("/" + entry["id"] + "/"):
                return {"artifact": deepcopy(entry)}
        raise AssertionError(path)


class GitHub:
    def __init__(self, fixture):
        self.calls = []
        self.fixture = fixture

    def request(self, method, path, payload=None, **kwargs):
        self.calls.append((method, path))
        if method != "GET" or payload is not None:
            raise AssertionError("Observer mutated GitHub")
        if self.fixture.run["native_outcome"] != "delivered":
            return []
        workspace = self.fixture.workspace
        return [
            {
                "number": 20,
                "head": {"sha": workspace["commit_sha"], "ref": workspace["branch"]},
                "base": {"ref": workspace["base_branch"]},
                "state": "open",
            }
        ]


class Fixture:
    def __init__(self, *, rounds=1, failure="checks", terminal=False):
        self.selected, self.run, collected, templates, _ = chain()
        self.selected["connection_id"] = 90
        self.selected["repair_limit"] = rounds - 1
        self.run.update(
            runner_id=7,
            state="completed",
            result_status="failed" if terminal else "succeeded",
            native_outcome="failed" if terminal else "delivered",
            result_url="" if terminal else "https://github.com/owner/repo/pull/20",
            error_summary=(
                "Factory shared repair allowance is exhausted." if terminal else ""
            ),
            source_snapshot={"issue_number": 99, "repository_id": 9},
        )
        snapshot = self.run["configuration_snapshot"]
        snapshot.update(execution_kind="native", autonomous=True)
        snapshot["execution_policy"]["repair_limit"] = rounds - 1
        snapshot["global"].update(
            task_timeout_seconds=600,
            research_timeout_seconds=100,
            validation_timeout_seconds=100,
        )
        snapshot["effective"]["factory"]["roles"] = {
            role: {"model": "model-" + role} for role in AGENT_ROLES
        }
        self.item = {
            "project": {"id": 7},
            "github_repository_id": 9,
            "issue_number": 99,
            "branch_name": collected["delivery"]["workspace"]["branch"],
        }
        account = {
            "backend": "codex",
            "credential_id": 90,
            "credential_version": 1,
            "model": "implementation-model",
        }
        context = {
            "run_id": self.run["id"],
            "account": account,
            "policy_digest": "d" * 64,
            "runtime_binding_digest": worker_identity()["binding_digest"],
            "repository": {
                "repository_id": 9,
                "repository": "owner/repo",
                "base_branch": "main",
                "branch": self.item["branch_name"],
            },
            "source": {
                "body": "Independent improvement",
                "title": "Improve source",
                "comments": [],
            },
        }
        self.context = {
            "id": identity(2),
            "document": context,
            "digest": digest(context),
        }
        self.run["source_snapshot"].update(context["source"])
        self.plan = collected["_factory_documents"]["plans"][identity(3)]
        self.workspace = collected["delivery"]["workspace"]
        self.statuses, self.artifacts, self.entries = {}, [], []
        self.next_id = 1000
        self.now = timestamp(self.run["created_at"]).timestamp() + 1
        self.validation_profile = ValidationProfile(
            "sha256:" + "a" * 64, "sha256:" + "b" * 64, "c" * 64
        )
        self.profile = profile_reference(self.validation_profile)
        for number in range(1, rounds + 1):
            failed = terminal or number < rounds
            self.add_round(templates, number, failed=failed, reason=failure)
        if not terminal:
            self.add_publication(rounds - 1)
        self.finish_history()
        self.queue = Queue(self)
        self.github = GitHub(self)
        self.worker = SimpleNamespace(
            status=lambda key: deepcopy(self.statuses[key]),
            capabilities=lambda: deepcopy(self.capabilities()),
        )

    def number(self):
        self.next_id += 1
        return self.next_id

    def entry(self, kind, value, *, operation=None):
        number = self.number()
        entry = {
            "id": number,
            "operation_id": operation or identity(number),
            "kind": kind,
            "data": value,
        }
        self.entries.append(entry)
        return entry

    def raw_status(self, operation, *, source="1" * 64, export="1" * 64, exit_code=0):
        start = self.now
        self.now += 0.1
        result = {
            "schema_version": 1,
            "run_id": self.run["id"],
            "operation_id": operation,
            "terminated": True,
            "exit_code": exit_code,
            "started_at": start,
            "ended_at": self.now,
            "log_digest": "d" * 64,
            "artifact": {"source_digest": source, "result_digest": export},
            "execution_identity": worker_identity(),
        }
        status = {
            "operation_id": operation,
            "state": "exited",
            "descendants_terminated": True,
            "execution_identity": worker_identity(),
            "worker_boot": worker_identity()["worker_boot"],
            "result": result,
        }
        self.statuses[operation] = status
        return status

    def agent(self, document):
        role = document["role"]
        execution = document["execution"]
        operation = identity(self.number())
        source = "2" * 64 if role == "build" else document["source_digest"]
        status = self.raw_status(
            operation, source=source, export=document["source_digest"]
        )
        manifest = {
            "schema_version": 1,
            "run_id": self.run["id"],
            "operation_id": operation,
            "profile_id": "factory-" + role,
            "profile_digest": "a" * 64,
            "source_digest": source,
            "context_digest": "3" * 64,
            "phase": "agent",
            "limits": {"cpu": 1, "memory_mb": 512, "pids": 64, "workspace_mb": 64},
            "deadline": "2026-11-22T00:10:00+00:00",
            "network_hosts": [],
        }
        account = {**self.context["document"]["account"], "model": "model-" + role}
        session = {
            "adapter": "codex",
            "agent_version": self.selected["agent_version"],
            "account": account,
            "auth_kind": "connection",
            "image_digest": "sha256:" + "b" * 64,
            "runtime_binding_digest": worker_identity()["binding_digest"],
            "manifest": manifest,
            "execution_identity": worker_identity(),
        }
        execution.update(
            operation_id=operation,
            model=account["model"],
            input_source_digest=source,
            started_at=status["result"]["started_at"],
            ended_at=status["result"]["ended_at"],
        )
        descriptor = {
            key: document[key]
            for key in ("role", "round", "specification_id", "specification_digest")
        }
        descriptor.update(
            {key: execution[key] for key in ("prompt_digest", "input_digest")}
        )
        intent = {
            "schema_version": 1,
            "kind": "agent",
            "context_id": self.context["id"],
            "context_digest": self.context["digest"],
            "policy_digest": "d" * 64,
            "attempt": document["round"] - 1,
            "data": {"factory": descriptor, "session": session, "workspace_id": None},
        }
        saved = self.entry("native_intent", intent)
        result = {
            "schema_version": 1,
            "intent_id": saved["operation_id"],
            "receipt": receipt_summary(status),
            "successful": True,
            "session_id": execution["session_id"],
            "agent_version": execution["agent_version"],
            "usage": [],
        }
        result = self.entry("native_agent", result)
        execution.update(
            intent_id=saved["operation_id"], result_id=result["operation_id"]
        )
        return saved["operation_id"]

    def add_round(self, templates, number, *, failed, reason):
        roles = templates if number == 1 else templates[2:]
        if failed and reason == "checks":
            roles = [
                entry
                for entry in roles
                if entry["role"] not in {"routing", "reviewer-code", "decision"}
            ]
        current = {}
        for template in roles:
            artifact = deepcopy(template)
            document = artifact["document"]
            document.update(
                operation_id=identity(self.number()),
                round=number,
                context_digest=self.context["digest"],
                configuration_digest=digest(self.run["configuration_snapshot"]),
            )
            if document["specification_id"]:
                document["specification_digest"] = self.plan["digest"]
            role = document["role"]
            if role == "reviewer-code" and failed:
                document["payload"].update(
                    verdict="changes",
                    findings=[
                        {
                            "id": "fix",
                            "summary": "Fix behavior",
                            "evidence": ["file.py:1"],
                            "criterion_ids": ["criterion-1"],
                        }
                    ],
                )
            artifact.update(
                id=identity(self.number()),
                sequence=len(self.artifacts) + 1,
                round=number,
                operation_id=document["operation_id"],
                context_id=self.context["id"],
            )
            if role == "checks":
                checks = []
                operations = []
                attempt_id = identity(self.number())
                validation_context = digest(
                    {
                        "schema_version": 1,
                        "kind": "validation",
                        "tree_sha": document["tree_sha"],
                        "source_digest": document["source_digest"],
                        "validation_profile_digest": self.validation_profile.digest,
                    }
                )
                for index, (check_id, fixed) in enumerate(
                    zip(CHECK_IDS, self.validation_profile.executor_profiles())
                ):
                    operation = str(uuid5(UUID(attempt_id), check_id))
                    status = self.raw_status(
                        operation,
                        exit_code=(
                            1 if failed and reason == "checks" and index == 0 else 0
                        ),
                    )
                    checks.append({"id": check_id, "receipt": receipt_summary(status)})
                    operations.append(
                        {
                            "id": check_id,
                            "manifest": {
                                "schema_version": 1,
                                "run_id": self.run["id"],
                                "operation_id": operation,
                                "profile_id": fixed.profile_id,
                                "profile_digest": fixed.digest,
                                "source_digest": document["source_digest"],
                                "context_digest": validation_context,
                                "phase": "validation",
                                "limits": fixed.limits.__dict__,
                                "deadline": "2026-11-22T00:10:00+00:00",
                                "network_hosts": [],
                            },
                        }
                    )
                session = {
                    "schema_version": 1,
                    "run_id": self.run["id"],
                    "attempt_id": attempt_id,
                    "profile": self.profile,
                    "profile_digest": self.validation_profile.digest,
                    "tree_sha": document["tree_sha"],
                    "source_digest": document["source_digest"],
                    "operations": operations,
                    "workspace_id": identity(444),
                    "provenance": None,
                }
                intent = self.entry(
                    "native_intent",
                    {
                        "schema_version": 1,
                        "kind": "validation",
                        "context_id": self.context["id"],
                        "context_digest": self.context["digest"],
                        "policy_digest": "d" * 64,
                        "attempt": number - 1,
                        "data": session,
                    },
                )
                validation = {
                    "schema_version": 1,
                    "intent_id": intent["operation_id"],
                    "run_id": self.run["id"],
                    "tree_sha": document["tree_sha"],
                    "source_digest": document["source_digest"],
                    "profile": self.profile,
                    "checks": checks,
                    "state": "failed" if failed and reason == "checks" else "passed",
                    "provenance": None,
                }
                saved = self.entry("native_validation", validation)
                document["payload"] = {
                    "validation_id": saved["operation_id"],
                    "state": validation["state"],
                }
            if role == "routing":
                document["payload"] = route(
                    self.run["configuration_snapshot"]["effective"]["factory"],
                    templates[0]["document"]["payload"],
                    ["work_queue/models.py"],
                )
            if role == "decision":
                document["payload"]["artifact_ids"] = [
                    current[key]["id"] for key in ("checks", "routing", "reviewer-code")
                ]
                if failed:
                    document["payload"]["verdict"] = "unresolved"
            agent_intent = None
            if "execution" in document:
                agent_intent = self.agent(document)
            artifact["digest"] = digest(document)
            if role == "specification":
                self.plan["document"].update(
                    factory_specification_id=artifact["id"],
                    factory_specification_digest=artifact["digest"],
                )
                self.plan["digest"] = digest(self.plan["document"])
            if role == "build":
                self.entry(
                    "native_workspace",
                    workspace_record(
                        context_id=self.context["id"],
                        context_digest=self.context["digest"],
                        attempt=number - 1,
                        agent_intent_id=agent_intent,
                        workspace=self.workspace,
                        source_digest=document["source_digest"],
                        export_digest=document["source_digest"],
                        requirements_digest="a" * 64,
                        factory_paths=["work_queue/models.py"],
                    ),
                )
            self.artifacts.append(artifact)
            current[role] = artifact

    def add_publication(self, attempt):
        publication_id = identity(self.number())
        intents = []
        for stage in ("push", "create_pr"):
            intent = {
                "schema_version": 1,
                "kind": "delivery",
                "context_id": self.context["id"],
                "context_digest": self.context["digest"],
                "policy_digest": "d" * 64,
                "attempt": attempt,
                "data": {
                    "publication_id": publication_id,
                    "stage": stage,
                    "workspace_id": identity(444),
                    "workspace": self.workspace,
                },
            }
            intents.append(self.entry("native_intent", intent)["operation_id"])
        pull = {
            "repository_id": 9,
            "number": 20,
            "url": self.run["result_url"],
            "base": "main",
            "head": self.workspace["branch"],
            "head_sha": self.workspace["commit_sha"],
        }
        self.entry(
            "native_delivery",
            {
                "schema_version": 1,
                "intent_ids": intents,
                "workspace_id": identity(444),
                "publication_id": publication_id,
                "workspace": self.workspace,
                "pushed_sha": self.workspace["commit_sha"],
                "pull_request": pull,
            },
        )
        receipt = delivery_receipt()
        receipt.update(
            run_id=self.run["id"],
            runner_id=7,
            configuration_revision=self.selected["configuration_revision"],
            context={"id": self.context["id"], "digest": self.context["digest"]},
            workspace=self.workspace,
        )
        receipt["validation"] = validation_evidence(
            next(
                entry["data"]
                for entry in reversed(self.entries)
                if entry["kind"] == "native_validation"
            )
        )
        receipt["delivery"].update(
            pull_request=pull, pushed_sha=self.workspace["commit_sha"]
        )
        self.entry("verified_delivery", receipt)

    def finish_history(self):
        self.entries = [
            entry for entry in self.entries if entry["kind"] != "native_termination"
        ]
        self.entry(
            "native_termination",
            termination_record(
                intent_ids=[
                    entry["operation_id"]
                    for entry in self.entries
                    if entry["kind"] == "native_intent"
                ],
                operations=list(self.statuses.values()),
                local_processes=[],
                publications=[],
            ),
        )

    def capabilities(self):
        return {
            **worker_identity(),
            "schema_version": 1,
            "qualification_version": 1,
            "factory_roles": {},
            "profiles": [],
            "qualified": True,
        }

    def pin(self):
        return {
            "run_id": self.run["id"],
            "context_id": self.context["id"],
            "context_digest": self.context["digest"],
            "evidence_ids": [entry["operation_id"] for entry in self.entries],
            "artifact_ids": [entry["id"] for entry in self.artifacts],
        }

    def history(self):
        return scenarios.History(
            self.pin(), queue=self.queue, worker=self.worker, scenario=self.selected
        )

    def verify(self, name, *, pin=None, records=()):
        proof = {
            "schema_version": 1,
            "operation_id": OPERATION,
            "cases": {name: pin or self.pin()},
        }
        return scenarios.verify_failures(
            proof,
            self.worker,
            queue=self.queue,
            github=self.github,
            scenario=self.selected,
            records=records,
            operation_id=OPERATION,
            checkpoint_digests=[
                digest(point) for point in (pin or {}).get("checkpoints", [])
            ],
        )

    def checkpoint(self, *, after=False, worker_restart=False):
        intent = next(
            entry["data"] for entry in self.entries if entry["kind"] == "native_intent"
        )
        session = intent["data"]["session"]
        operation = session["manifest"]["operation_id"]
        observed = deepcopy(self.statuses[operation])
        if not after:
            observed.update(state="running", descendants_terminated=False)
            observed.pop("result")
        elif worker_restart:
            observed["result"].update(
                worker_restart=True,
                reconciled_boot=identity(999),
                started_at=None,
                exit_code=None,
            )
            observed["result"].pop("artifact")
            self.statuses[operation] = deepcopy(observed)
        receipt = receipt_summary(observed) if after else None
        record = {
            "binding": {
                "role": "intake",
                "context_id": self.context["id"],
                "context_digest": self.context["digest"],
            },
            "session": {
                "manifest": session["manifest"],
                "execution_identity": worker_identity(),
            },
            "requested_deadline": 1795306200.0,
            "deadline": "2026-11-22T00:10:00+00:00",
            "dispatched_at": self.now - 0.5,
            "elapsed": 0.2 if after else 0.1,
            "state": "exited" if after else "running",
        }
        if after:
            record["receipt_digest"] = receipt["receipt_digest"]
        caps = self.capabilities()
        if after and worker_restart:
            caps["worker_boot"] = identity(999)
        return {
            "schema_version": 1,
            "kind": "factory-observation",
            "operation_id": OPERATION,
            "container": "disposable-runner",
            "observed_at": self.now + (1 if after else 0),
            "runner_id": 7,
            "run_id": self.run["id"],
            "context_id": self.context["id"],
            "context_digest": self.context["digest"],
            "controller": {
                "kind": "actual-controller-process",
                "runner_id": 7,
                "uid": 10001,
                "pid": 2,
                "start_ticks": 22 if after else 11,
            },
            "capabilities": caps,
            "statuses": {operation: observed},
            "native": {
                "run_id": self.run["id"],
                "context": self.context,
                "plan": self.plan,
                "attempt": 0,
                "deadline": "2026-11-22T00:10:00+00:00",
                "factory": {
                    "recovery": {
                        "frozen": {"deadline": 1795306200.0},
                        "operations": {operation: record},
                        "repairs": [],
                    }
                },
            },
            "budget": {
                "limits": {"total": 600},
                "total": 0.2 if after else 0.1,
                "stages": {"implementation": 0.2 if after else 0.1},
                "charges": {
                    operation: {
                        "stage": "implementation",
                        "elapsed": 0.2 if after else 0.1,
                    }
                },
            },
        }


class FactoryScenarioTests(unittest.TestCase):
    def assert_case_passes(self, fixture, name, **kwargs):
        self.assertFalse(
            any(name in message for message in fixture.verify(name, **kwargs))
        )
        self.assertTrue(
            all(
                method == "GET"
                for method, _ in fixture.queue.calls + fixture.github.calls
            )
        )

    def test_failed_check_then_bounded_repair_has_positive_complete_proof(self):
        fixture = Fixture(rounds=2)
        self.assert_case_passes(fixture, "failed-check-repair")

    def test_review_requested_changes_then_repair_has_positive_complete_proof(self):
        fixture = Fixture(rounds=2, failure="reviews")
        self.assert_case_passes(fixture, "review-request-repair")

    def test_truthful_exhaustion_has_positive_complete_proof(self):
        fixture = Fixture(rounds=2, terminal=True)
        self.assert_case_passes(fixture, "repair-exhaustion")

    def test_missing_required_reviewer_has_positive_complete_proof(self):
        fixture = Fixture(terminal=True, failure="reviews")
        fixture.artifacts = [
            entry
            for entry in fixture.artifacts
            if entry["role"] not in {"routing", "reviewer-code", "decision"}
        ]
        fixture.entries = [
            entry
            for entry in fixture.entries
            if not (
                entry["kind"] == "native_intent"
                and entry["data"]["data"].get("factory", {}).get("role")
                in {"reviewer-code", "decision"}
            )
        ]
        intents = {
            entry["operation_id"]
            for entry in fixture.entries
            if entry["kind"] == "native_intent"
        }
        fixture.entries = [
            entry
            for entry in fixture.entries
            if entry["kind"] != "native_agent" or entry["data"]["intent_id"] in intents
        ]
        operations = {
            entry["data"]["receipt"]["operation_id"]
            for entry in fixture.entries
            if entry["kind"] == "native_agent"
        }
        operations.update(
            check["receipt"]["operation_id"]
            for entry in fixture.entries
            if entry["kind"] == "native_validation"
            for check in entry["data"]["checks"]
        )
        fixture.statuses = {
            key: value for key, value in fixture.statuses.items() if key in operations
        }
        fixture.finish_history()
        fixture.run["error_summary"] = (
            "Factory reviewer-code has no qualified worker profile."
        )
        point = fixture.checkpoint()
        pin = {**fixture.pin(), "checkpoints": [point]}
        self.assert_case_passes(fixture, "missing-capability", pin=pin)

    def test_protected_cancellation_has_positive_complete_proof(self):
        fixture = Fixture(terminal=True)
        fixture.run.update(
            native_outcome="canceled",
            cancellation_requested_at="2026-11-22T00:00:01+00:00",
            cancellation_confirmed_at="2026-11-22T00:01:00+00:00",
        )
        operation = next(iter(fixture.statuses))
        fixture.statuses[operation]["result"]["canceled"] = True
        fixture.artifacts = []
        fixture.statuses = {operation: fixture.statuses[operation]}
        fixture.entries = [
            entry
            for entry in fixture.entries
            if entry["kind"] == "native_intent"
            and entry["data"]["kind"] == "agent"
            and entry["data"]["data"]["session"]["manifest"]["operation_id"]
            == operation
        ]
        fixture.finish_history()
        self.assert_case_passes(fixture, "cancellation")

    def test_controller_restart_has_positive_complete_proof(self):
        fixture = Fixture()
        pin = {
            **fixture.pin(),
            "checkpoints": [fixture.checkpoint(), fixture.checkpoint(after=True)],
        }
        self.assert_case_passes(fixture, "controller-restart", pin=pin)

    def test_worker_restart_has_positive_complete_proof(self):
        fixture = Fixture()
        before = fixture.checkpoint()
        after = fixture.checkpoint(after=True, worker_restart=True)
        fixture.artifacts = []
        fixture.entries = [
            entry for entry in fixture.entries if entry["kind"] != "native_agent"
        ]
        fixture.finish_history()
        pin = {**fixture.pin(), "checkpoints": [before, after]}
        self.assert_case_passes(fixture, "worker-restart", pin=pin)

    def test_independent_work_during_released_review_wait_has_positive_complete_proof(
        self,
    ):
        fixture = Fixture()
        first = {
            "run_id": identity(888),
            "claim_released": True,
            "finished_at": "2026-11-21T23:59:00+00:00",
            "human_merge": {
                "status": "human_merged",
                "merged_at": "2026-11-22T00:03:00+00:00",
            },
        }
        self.assert_case_passes(
            fixture, "independent-work-during-human-review", records=[first]
        )

    def test_empty_and_label_only_proof_never_pass(self):
        fixture = Fixture()
        missing = scenarios.verify_failures(
            {},
            fixture.worker,
            queue=fixture.queue,
            github=fixture.github,
            scenario=fixture.selected,
            records=[],
            operation_id=OPERATION,
        )
        self.assertEqual(len(missing), 8)
        with self.assertRaisesRegex(ValueError, "labels"):
            fixture.verify(
                "cancellation", pin={"passed": True, "fixtures": False, "receipts": []}
            )

    def test_original_receipt_with_wrong_run_is_rejected(self):
        fixture = Fixture()
        next(iter(fixture.statuses.values()))["result"]["run_id"] = identity(9999)
        with self.assertRaisesRegex(ValueError, "authenticated original"):
            fixture.verify("controller-restart")

    def test_wrong_context_and_artifact_history_are_rejected(self):
        fixture = Fixture(rounds=2)
        pin = {**fixture.pin(), "context_digest": "f" * 64}
        with self.assertRaisesRegex(ValueError, "context"):
            fixture.verify("failed-check-repair", pin=pin)
        pin = {**fixture.pin(), "artifact_ids": []}
        with self.assertRaisesRegex(ValueError, "history"):
            fixture.verify("failed-check-repair", pin=pin)

    def test_passed_label_on_failed_check_is_rejected(self):
        fixture = Fixture(rounds=2)
        entry = next(entry for entry in fixture.artifacts if entry["role"] == "checks")
        entry["document"]["payload"]["state"] = "passed"
        entry["digest"] = digest(entry["document"])
        with self.assertRaisesRegex(ValueError, "validation source or outcome"):
            fixture.verify("failed-check-repair")

    def test_wrong_candidate_check_evidence_is_rejected(self):
        fixture = Fixture(rounds=2)
        entry = next(
            entry for entry in fixture.entries if entry["kind"] == "native_validation"
        )
        entry["data"]["source_digest"] = "f" * 64
        with self.assertRaisesRegex(ValueError, "validation changed its original"):
            fixture.verify("failed-check-repair")

    def test_wrong_original_operation_is_rejected(self):
        fixture = Fixture(rounds=2)
        next(entry for entry in fixture.artifacts if entry["role"] == "build")[
            "document"
        ]["execution"]["operation_id"] = identity(7777)
        fixture.artifacts[2]["digest"] = digest(fixture.artifacts[2]["document"])
        with self.assertRaisesRegex(ValueError, "genuine completed execution"):
            fixture.verify("failed-check-repair")

    def test_restart_budget_reset_is_rejected(self):
        fixture = Fixture()
        points = [fixture.checkpoint(), fixture.checkpoint(after=True)]
        points[1]["budget"]["total"] = 0
        with self.assertRaisesRegex(ValueError, "cumulative budget"):
            fixture.verify(
                "controller-restart", pin={**fixture.pin(), "checkpoints": points}
            )

    def test_restart_replaced_dispatch_is_rejected(self):
        fixture = Fixture()
        points = [fixture.checkpoint(), fixture.checkpoint(after=True)]
        next(iter(points[1]["native"]["factory"]["recovery"]["operations"].values()))[
            "dispatched_at"
        ] += 1
        with self.assertRaisesRegex(ValueError, "dispatch binding"):
            fixture.verify(
                "controller-restart", pin={**fixture.pin(), "checkpoints": points}
            )

    def test_worker_restart_requires_actual_new_boot(self):
        fixture = Fixture()
        points = [fixture.checkpoint(), fixture.checkpoint(after=True)]
        with self.assertRaisesRegex(ValueError, "did not change boot"):
            fixture.verify(
                "worker-restart", pin={**fixture.pin(), "checkpoints": points}
            )

    def test_independent_work_after_merge_is_rejected(self):
        fixture = Fixture()
        first = {
            "run_id": identity(888),
            "claim_released": True,
            "finished_at": "2026-11-21T23:59:00+00:00",
            "human_merge": {
                "status": "human_merged",
                "merged_at": "2026-11-22T00:01:00+00:00",
            },
        }
        with self.assertRaisesRegex(ValueError, "strictly within"):
            fixture.verify("independent-work-during-human-review", records=[first])

    def test_capture_is_private_finite_and_never_dispatches(self):
        fixture = Fixture()
        point = fixture.checkpoint()
        with tempfile.TemporaryDirectory() as directory:
            import json

            path = Path(directory) / "journal.json"
            point["controller"]["journal_path"] = str(path)
            path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "runner_id": 7,
                        "native": point["native"],
                        "budgets": {fixture.run["id"]: point["budget"]},
                    }
                )
            )
            path.chmod(0o600)
            with patch.object(
                scenarios, "controller_witness", return_value=point["controller"]
            ):
                captured = scenarios.capture_checkpoint(
                    path, worker=fixture.worker, operation_id=OPERATION
                )
            self.assertEqual(captured["run_id"], fixture.run["id"])
            self.assertEqual(
                captured["native"]["deadline"], point["native"]["deadline"]
            )
            path.chmod(0o644)
            with self.assertRaisesRegex(ValueError, "private"):
                scenarios.capture_checkpoint(
                    path, worker=fixture.worker, operation_id=OPERATION
                )


class CheckpointAuthenticationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = Fixture()
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "journal.json"
        point = self.fixture.checkpoint()
        point["controller"]["journal_path"] = str(self.path)
        self.journal = {
            "version": 1,
            "runner_id": 7,
            "native": point["native"],
            "budgets": {self.fixture.run["id"]: point["budget"]},
        }
        self.write_journal()
        witness = patch.object(
            scenarios, "controller_witness", return_value=point["controller"]
        )
        witness.start()
        self.addCleanup(witness.stop)
        self.point = scenarios.capture_checkpoint(
            self.path, worker=self.fixture.worker, operation_id=OPERATION
        )

    def write_journal(self):
        import json

        self.path.write_text(json.dumps(self.journal))
        self.path.chmod(0o600)

    def authenticate(self, point=None):
        return scenarios.authenticate_checkpoint(
            point or self.point, self.fixture.worker, journal_path=self.path
        )

    def test_current_trusted_capture_authenticates_without_mutation(self):
        self.assertEqual(self.authenticate(), digest(self.point))

    def test_stale_capture_cannot_be_registered_later(self):
        self.point["observed_at"] -= 31
        with self.assertRaisesRegex(ValueError, "while its observation is current"):
            self.authenticate()

    def test_forged_worker_capability_is_rejected(self):
        self.point["capabilities"]["qualified"] = False
        with self.assertRaisesRegex(ValueError, "actual trusted"):
            self.authenticate()

    def test_boolean_replacing_integer_is_rejected(self):
        self.point["schema_version"] = True
        with self.assertRaisesRegex(ValueError, "actual trusted"):
            self.authenticate()

    def test_changed_journal_cannot_authenticate_old_checkpoint(self):
        self.journal["native"]["attempt"] += 1
        self.write_journal()
        with self.assertRaisesRegex(ValueError, "actual trusted"):
            self.authenticate()

    def test_changed_original_status_cannot_authenticate_old_checkpoint(self):
        operation = next(iter(self.point["statuses"]))
        self.fixture.statuses[operation]["result"]["exit_code"] = 7
        with self.assertRaisesRegex(ValueError, "actual trusted"):
            self.authenticate()

    def test_foreign_context_cannot_authenticate(self):
        self.point["context_id"] = identity(7000)
        with self.assertRaisesRegex(ValueError, "actual trusted"):
            self.authenticate()

    def test_unregistered_checkpoint_never_proves_restart(self):
        pin = {**self.fixture.pin(), "checkpoints": [self.point]}
        proof = {
            "schema_version": 1,
            "operation_id": OPERATION,
            "cases": {"controller-restart": pin},
        }
        missing = scenarios.verify_failures(
            proof,
            self.fixture.worker,
            queue=self.fixture.queue,
            github=self.fixture.github,
            scenario=self.fixture.selected,
            records=[],
            operation_id=OPERATION,
        )
        self.assertTrue(
            any(
                "controller-restart" in message
                and "authenticated and pinned" in message
                for message in missing
            )
        )

    def test_foreign_acceptance_operation_is_rejected(self):
        proof = {
            "schema_version": 1,
            "operation_id": identity(7000),
            "cases": {"controller-restart": self.fixture.pin()},
        }
        with self.assertRaisesRegex(ValueError, "foreign failure-matrix"):
            scenarios.verify_failures(
                proof,
                self.fixture.worker,
                queue=self.fixture.queue,
                github=self.fixture.github,
                scenario=self.fixture.selected,
                records=[],
                operation_id=OPERATION,
            )

    def test_private_copy_cannot_replace_registered_controller_journal(self):
        copied = self.path.parent / "copied-journal.json"
        copied.write_bytes(self.path.read_bytes())
        copied.chmod(0o600)
        with self.assertRaisesRegex(ValueError, "registered journal"):
            scenarios.capture_checkpoint(
                copied, worker=self.fixture.worker, operation_id=OPERATION
            )


class FinalValidationBindingTests(unittest.TestCase):
    def test_final_passed_check_requires_authenticated_original_operation(self):
        fixture = Fixture(rounds=2)
        final = next(
            entry["data"]
            for entry in reversed(fixture.entries)
            if entry["kind"] == "native_validation"
        )
        final["checks"][0]["receipt"]["operation_id"] = identity(99999999)
        verified = next(
            entry["data"]
            for entry in fixture.entries
            if entry["kind"] == "verified_delivery"
        )
        verified["validation"] = validation_evidence(final)
        with self.assertRaisesRegex(ValueError, "authenticated original termination"):
            fixture.verify("failed-check-repair")

    def test_final_passed_validation_cannot_change_source(self):
        fixture = Fixture(rounds=2)
        final = next(
            entry["data"]
            for entry in reversed(fixture.entries)
            if entry["kind"] == "native_validation"
        )
        final["source_digest"] = "f" * 64
        with self.assertRaisesRegex(ValueError, "changed its original"):
            fixture.verify("failed-check-repair")

    def test_final_passed_validation_cannot_use_missing_intent(self):
        fixture = Fixture(rounds=2)
        final = next(
            entry["data"]
            for entry in reversed(fixture.entries)
            if entry["kind"] == "native_validation"
        )
        final["intent_id"] = identity(99999999)
        with self.assertRaisesRegex(ValueError, "original immutable intent"):
            fixture.verify("failed-check-repair")
