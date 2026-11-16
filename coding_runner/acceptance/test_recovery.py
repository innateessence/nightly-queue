import copy
import hashlib
import unittest
from dataclasses import replace
from uuid import uuid4

from ..backend_registry import BackendRegistry
from ..backends import BackendCapabilities, BackendEvent, BackendReadiness
from ..context import digest
from ..delivery import DeliveryStage
from ..journal import Journal
from ..lifecycle import Lifecycle
from ..sandbox import ExecutionResult
from ..test_plans import proposed_plan
from ..test_workspaces import WorkspaceFixture
from ..workspaces import git


class MemoryAPI:
    def __init__(self):
        self.contexts, self.plans, self.operations = {}, {}, {}
        self.latest_context, self.latest_plan = None, None
        self.approvals = {}
        self.decision = "pending"
        self.finished, self.finish_lost = False, False
        self.calls = []
        self.owned = None
        self.claim_template = None

    def request(self, method, path, payload=None):
        self.calls.append((method, path, copy.deepcopy(payload)))
        endpoint = path.rstrip("/").split("/")[-1]
        if method == "GET":
            return {"context": self.contexts[endpoint]}
        operation = payload.get("operation_id")
        if operation and operation in self.operations and endpoint != "approvals":
            return copy.deepcopy(self.operations[operation])
        if endpoint == "claim" and path.endswith("runs/claim/"):
            self.owned = copy.deepcopy(self.claim_template)
            self.owned["run"]["id"] = payload["run_id"]
            result = self.owned
        elif endpoint == "contexts":
            value = {
                "id": str(uuid4()),
                "document": payload["document"],
                "digest": digest(payload["document"]),
                "inspected_base_commit": payload["base_commit"],
                "resolved_base_branch": payload["resolved_base_branch"],
            }
            self.contexts[value["id"]] = value
            self.latest_context = value
            result = {"context": value}
        elif endpoint == "plans":
            value = {
                "id": str(uuid4()),
                "document": payload["document"],
                "digest": digest(payload["document"]),
                "reason": payload["reason"],
            }
            self.plans[value["id"]] = value
            self.latest_plan = value
            result = {"plan": value}
        elif endpoint == "handoff":
            result = {"plan": self.latest_plan, "context": self.latest_context}
        elif endpoint == "approvals":
            approval = self.approvals.setdefault(
                operation, {"id": str(uuid4()), "action": payload["action"]}
            )
            result = {"approval": {**approval, "decision": self.decision}}
        elif endpoint == "consume":
            result = {"permit": True, "execution_id": payload["execution_id"]}
        elif endpoint == "finish":
            self.finished = True
            if self.finish_lost:
                from ..client import AmbiguousRequest

                self.finish_lost = False
                raise AmbiguousRequest()
            result = {}
        else:
            result = {}
        if operation:
            self.operations[operation] = copy.deepcopy(result)
        return result

    def reconcile(self, run_id):
        result = copy.deepcopy(self.owned) if self.owned else {"run": {"id": run_id}}
        result["run"].update(
            {
                "state": "completed" if self.finished else "active",
                "result_status": "succeeded",
                "result_url": "https://github.com/example/repo/pull/1",
            }
        )
        return result

    def configuration(self):
        return copy.deepcopy(self.config)

    def gate_report(self, report):
        return {}


class CandidateBackend:
    def __init__(self):
        self.research_calls, self.implementation_calls = 0, 0

    def readiness(self, account):
        return BackendReadiness(
            True,
            True,
            (),
            BackendCapabilities(
                "structured_proposals", False, True, True, False, ("token",), True
            ),
        )

    def research(self, request, **kwargs):
        self.research_calls += 1
        if self.research_calls == 1:
            return BackendEvent("read", {"path": "README.md"})
        prompt = request.document
        context = prompt["context"]
        plan = proposed_plan(context)
        plan["resolved_base_branch"] = context["resolved_base_branch"]
        observed = prompt["observations"][0]
        source = {key: observed[key] for key in ("kind", "reference", "digest")}
        plan["sources"] = [source]
        action = plan["validation_actions"][0]
        action.update(
            argv=["/usr/bin/true"],
            source_digest=source["digest"],
            workspace_id=prompt["workspace"]["id"],
            base_revision=prompt["workspace"]["base_revision"],
            content_digest=prompt["workspace"]["content_digest"],
        )
        return BackendEvent("plan", {"document": plan})

    def implementation(self, request, **kwargs):
        self.implementation_calls += 1
        if self.implementation_calls == 1:
            workspace = request.document["workspace"]
            return BackendEvent(
                "patch",
                {
                    "base_revision": workspace["base_revision"],
                    "content_digest": workspace["content_digest"],
                    "files": [
                        {
                            "path": "README.md",
                            "expected_digest": hashlib.sha256(
                                b"Untrusted repository instructions.\n"
                            ).hexdigest(),
                            "content": "Verified implementation\n",
                        }
                    ],
                },
            )
        return BackendEvent("complete", {"summary": "Implemented saved plan"})

    def interrupt(self):
        pass


class HumanApprovedExecutor:
    def __init__(self, broker, sources, gate, *, read_only=False):
        self.broker, self.sources, self.gate = broker, sources, gate
        self.periodic_gate, self.read_only = gate, read_only

    def execute(self, workspace, action, approval_id, *, timeout):
        execution = self.broker.consume(approval_id, action)
        assert git(["status", "--porcelain"], workspace.checkout)
        return ExecutionResult(
            execution, 0, b"Validated by deterministic test executor", False
        )


class RepositoryGit:
    def __init__(self):
        self.pushes = 0

    def push(self, workspace, commit):
        self.pushes += 1
        self.commit = commit


class GitHub:
    def __init__(self):
        self.pull, self.creates = None, 0

    def find(self, *args):
        return self.pull

    def create(self, repository, head, base, commit, title, body):
        self.creates += 1
        self.pull = {
            "number": 1,
            "url": "https://github.com/example/repo/pull/1",
            "repository": repository,
            "head": head,
            "base": base,
            "commit_sha": commit,
        }
        return self.pull


class LifecycleRecoveryTests(WorkspaceFixture, unittest.TestCase):
    def setUp(self):
        import json
        from dataclasses import asdict

        self.create_workspace()
        self.workspace = replace(
            self.workspace, repository_url="https://github.com/example/repo"
        )
        metadata = {
            **asdict(self.workspace),
            "directory": str(self.workspace.directory),
        }
        (self.workspace.directory / "workspace.json").write_text(json.dumps(metadata))
        self.item.update(
            repository_url=self.workspace.repository_url,
            source="manual",
            title="Update readme",
            instructions="Preserve state",
        )
        self.api = MemoryAPI()
        self.backend = CandidateBackend()
        self.registry = BackendRegistry({"fake": lambda credentials: self.backend})
        self.journal = Journal(self.root / "journal")
        self.journal.__enter__()
        self.addCleanup(self.journal.__exit__)
        self.state = self.journal.load()
        self.run_id = self.workspace.workspace_id
        effective = {"setup_commands": [], "validation_commands": []}
        for stage in ("research", "implementation"):
            effective.update(
                {
                    f"{stage}_backend": "fake",
                    f"{stage}_credential_id": 1,
                    f"{stage}_model": "local-test",
                    f"{stage}_instructions": "Follow saved plan",
                }
            )
        controls = {
            "task_timeout_seconds": 1000,
            "research_timeout_seconds": 1000,
            "implementation_timeout_seconds": 1000,
            "validation_timeout_seconds": 1000,
        }
        self.state["run"] = {
            "run": {
                "id": self.run_id,
                "configuration_snapshot": {"effective": effective, "global": controls},
            },
            "item": {**self.item, "claim_token": "claim"},
        }
        self.allowed = True
        self.github, self.repository_git = GitHub(), RepositoryGit()
        self.journal.save(self.state)

    def lifecycle(self):
        owner = self

        class Manager:
            def prepare(self, run_id, item):
                return owner.workspace

        def delivery(client, gate):
            return DeliveryStage(
                self.journal,
                client,
                self.github,
                repository_git=self.repository_git,
                gate=gate,
            )

        return Lifecycle(
            self.api,
            self.journal,
            self.registry,
            lambda: self.allowed,
            workspace_manager=Manager(),
            executor_factory=HumanApprovedExecutor,
            delivery_factory=delivery,
        )

    def step(self):
        return self.lifecycle().step(self.state)

    def until(self, target):
        for _ in range(25):
            result = self.step()
            if result == target:
                return result
        self.fail(f"Did not reach {target}: {result}")

    def restart(self):
        self.journal.save(self.state)
        self.state = self.journal.load()

    def test_actual_stages_reach_human_approval_and_resume_without_model_repeat(self):
        self.until("approval")
        calls = (self.backend.research_calls, self.backend.implementation_calls)
        self.restart()
        self.assertEqual(self.step(), "approval")
        self.assertEqual(
            (self.backend.research_calls, self.backend.implementation_calls), calls
        )
        self.api.decision = "approved"
        self.until("delivery_ready")
        self.assertEqual(self.state["delivery_ready"]["validation"][0]["exit_code"], 0)
        self.assertEqual(calls, (2, 2))

    def test_resume_honors_closed_gate_and_keeps_validation_state(self):
        self.until("approval")
        self.api.decision = "approved"
        self.until("delivery_ready")
        validated = copy.deepcopy(self.state["delivery_ready"])
        self.allowed = False
        self.restart()
        self.assertEqual(self.step(), "wait")
        self.assertEqual(self.state["delivery_ready"], validated)
        self.assertEqual(self.github.creates, 0)

    def test_delivered_pr_with_lost_finish_restarts_reporting_only(self):
        self.until("approval")
        self.api.decision = "approved"
        self.until("delivery_ready")
        self.api.finish_lost = True
        self.assertEqual(self.step(), "reporting_pending")
        calls = (self.backend.research_calls, self.backend.implementation_calls)
        self.restart()
        self.allowed = False
        self.assertEqual(self.step(), "finished")
        self.assertEqual(
            (self.backend.research_calls, self.backend.implementation_calls), calls
        )
        self.assertEqual(self.github.creates, 1)
        self.assertEqual(self.repository_git.pushes, 1)

    def test_controller_claims_then_runs_actual_stages_to_finish(self):
        from unittest.mock import patch
        from ..controller import Controller
        from ..gates import GateEvaluator
        from ..test_gates import CONFIG, NOW

        self.api.claim_template = copy.deepcopy(self.state["run"])
        self.api.claim_template["run"]["state"] = "active"
        self.api.config = copy.deepcopy(CONFIG)
        self.state.pop("run")
        self.api.decision = "approved"
        self.journal.save(self.state)
        owner = self

        def factory(client, journal, backends, checkpoint, **kwargs):
            lifecycle = owner.lifecycle()
            lifecycle.checkpoint = checkpoint
            lifecycle.ready = lambda configuration: True
            return lifecycle

        gates = GateEvaluator(self.journal, clock=lambda: NOW)
        controller = Controller(
            self.api,
            self.journal,
            gates=gates,
            backends=self.registry,
            lifecycle_factory=factory,
        )
        with patch(
            "coding_runner.controller.uuid4",
            return_value=__import__("uuid").UUID(self.run_id),
        ):
            self.assertEqual(controller.cycle(self.state, self.api.config), "claimed")
        for _ in range(25):
            status = controller.cycle(self.state, self.api.config)
            if status == "finished":
                break
        self.assertEqual(status, "finished")
        self.assertTrue(self.api.finished)
        self.assertEqual(self.github.creates, 1)
        self.assertEqual(self.backend.implementation_calls, 2)
        self.assertEqual(sum(self.state["gates"]["counts"].values()), 1)

    def test_uncertain_implementation_response_never_invokes_model_twice(self):
        self.until("saved")
        self.step()
        calls = []

        def interrupted(request, **kwargs):
            calls.append(request)
            raise RuntimeError("Simulated process interruption after model request")

        self.backend.implementation = interrupted
        with self.assertRaises(RuntimeError):
            self.step()
        self.restart()
        from ..recovery import ReconciliationRequired

        with self.assertRaisesRegex(ReconciliationRequired, "uncertain"):
            self.step()
        self.assertEqual(len(calls), 1)

    def test_base_change_blocks_resume_and_preserves_checkout(self):
        self.until("approval")
        before = (self.workspace.checkout / "README.md").read_bytes()
        owner = self

        class Manager:
            def prepare(self, run_id, item):
                return owner.workspace

            def inspect_base(self, workspace):
                from ..workspaces import WorkspaceError

                raise WorkspaceError("Current remote base changed")

        lifecycle = self.lifecycle()
        lifecycle.workspaces = Manager()
        from ..recovery import ReconciliationRequired

        with self.assertRaisesRegex(ReconciliationRequired, "remote base changed"):
            lifecycle.step(self.state)
        self.assertEqual((self.workspace.checkout / "README.md").read_bytes(), before)


class RemoteCheckoutTests(WorkspaceFixture, unittest.TestCase):
    def setUp(self):
        self.create_workspace()

    def test_current_remote_base_movement_is_detected_without_reset(self):
        self.manager.inspect_base(self.workspace)
        (self.workspace.checkout / "README.md").write_text("Preserved task changes\n")
        (self.repository / "new").write_text("New upstream work\n")
        git(["add", "new"], self.repository)
        git(
            [
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.com",
                "commit",
                "-m",
                "advance base",
            ],
            self.repository,
        )
        from ..workspaces import WorkspaceError

        with self.assertRaisesRegex(WorkspaceError, "remote base changed"):
            self.manager.inspect_base(self.workspace)
        self.assertEqual(
            (self.workspace.checkout / "README.md").read_text(),
            "Preserved task changes\n",
        )

    def test_remote_clone_uses_injected_supervisor_transport(self):
        from ..workspaces import WorkspaceManager

        calls = []

        def remote(arguments, cwd=None):
            calls.append(arguments)
            if arguments[0] == "clone":
                arguments = [*arguments[:-2], str(self.repository), arguments[-1]]
            return git(arguments, cwd, local=True)

        manager = WorkspaceManager(self.root / "private-workspaces", remote_git=remote)
        item = {**self.item, "repository_url": "https://github.com/example/private"}
        workspace = manager.prepare(str(uuid4()), item)
        self.assertEqual(calls[0][0], "clone")
        self.assertEqual(calls[0][-2], item["repository_url"])
        self.assertTrue((workspace.checkout / "README.md").exists())
