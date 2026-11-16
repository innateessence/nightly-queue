import copy
import hashlib
import unittest
from dataclasses import replace
from uuid import uuid4

from ..approvals import digest
from ..backends import (
    BackendCapabilities,
    BackendError,
    BackendEvent,
    BackendReadiness,
    BackendRequest,
)
from ..budgets import ActiveBudget, BudgetExceeded
from ..context import manual_context
from ..implementation import (
    ImplementationStage,
    apply_patch,
    verify_delivery_ready,
    workspace_evidence,
)
from ..journal import Journal
from ..sandbox import ApprovedExecutor, ExecutionResult
from ..test_workspaces import WorkspaceFixture
from ..workspaces import WorkspaceError, git


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class Broker:
    def __init__(self, run_id):
        self.run_id = run_id
        self.decision = "pending"
        self.requests = []
        self.consumed = []

    def request(self, action, operation_id):
        self.requests.append((action, operation_id))
        return {"id": str(uuid4()), "decision": self.decision}

    def consume(self, approval_id, action):
        if self.decision != "approved":
            raise AssertionError("Test broker requires human approval")
        self.consumed.append(action)
        return str(uuid4())


class Executor:
    def __init__(self, broker, clock):
        self.broker, self.clock = broker, clock
        self.calls = []
        self.exit_codes = []
        self.gate = lambda: True
        self.periodic_gate = lambda: True
        self.change = None
        self.elapsed = 1.0

    def execute(self, workspace, action, approval_id, *, timeout):
        self.broker.consume(approval_id, action)
        self.calls.append((action, timeout))
        self.clock.now += min(timeout, self.elapsed)
        if self.change:
            self.change(workspace)
            self.change = None
        code = self.exit_codes.pop(0) if self.exit_codes else 0
        return ExecutionResult(str(uuid4()), code, b"test output", False)


class Backend:
    def __init__(self, clock):
        self.clock, self.calls = clock, 0
        self.events = []
        self.interruptions = 0
        self.elapsed = 1.0

    def readiness(self, account):
        caps = BackendCapabilities(
            "structured_proposals", False, True, True, False, ("token",), True
        )
        return BackendReadiness(True, True, (), caps)

    def implementation(self, request, *, progress, checkpoint, stop):
        self.calls += 1
        self.clock.now += min(request.timeout_seconds, self.elapsed)
        if not checkpoint():
            raise BudgetExceeded("Active budget exhausted during model execution.")
        event = self.events.pop(0)
        return event(request) if callable(event) else event

    def interrupt(self):
        self.interruptions += 1


class Store:
    def __init__(self, item, workspace):
        context = manual_context(item)
        self.context = {
            "id": str(uuid4()),
            "document": context,
            "digest": digest(context),
            "inspected_base_commit": workspace.base_revision,
        }
        self.origins = {self.context["id"]: self.context}
        self.saved = []
        self.client = self
        self.failure = None
        self.refreshes = 0
        self.plan = None

    def request(self, method, path, **kwargs):
        context_id = path.rstrip("/").split("/")[-1]
        return {"context": self.origins[context_id]}

    def handoff(self, item, base, **kwargs):
        self.refreshes += 1
        if self.failure:
            raise self.failure
        return {"plan": self.plan, "context": self.context}

    def save_plan(self, document, context, *, reason, operation_id):
        self.plan = {
            "id": str(uuid4()),
            "document": document,
            "digest": digest(document),
            "reason": reason,
        }
        self.saved.append(self.plan)
        return self.plan


class ImplementationTests(WorkspaceFixture, unittest.TestCase):
    def setUp(self):
        self.create_workspace()
        self.item.update(
            source="manual", title="Update value", instructions="Change value safely"
        )
        self.run_id = self.workspace.workspace_id
        self.journal = Journal(self.root / "journal")
        self.journal.__enter__()
        self.addCleanup(self.journal.__exit__, None, None, None)
        self.state = self.journal.load()
        self.clock = Clock()
        self.budget = ActiveBudget(
            self.journal,
            self.state,
            self.run_id,
            100,
            {"research": 50, "implementation": 50, "validation": 50},
            clock=self.clock,
        )
        self.broker = Broker(self.run_id)
        self.executor = Executor(self.broker, self.clock)
        self.backend = Backend(self.clock)
        self.store = Store(self.item, self.workspace)
        action = self.action()
        source = {
            "kind": action.source_kind,
            "reference": action.source_reference,
            "digest": action.source_digest,
        }
        context = self.store.context
        document = {
            "schema_version": 1,
            "context_id": context["id"],
            "context_digest": context["digest"],
            "problem": "Value is outdated",
            "expected_behavior": "Value is current",
            "acceptance_criteria": ["Expected value"],
            "findings": [],
            "sources": [source],
            "relevant_paths": ["README.md"],
            "approach": "Edit value",
            "tradeoffs": [],
            "steps": ["Update value"],
            "file_changes": ["README.md"],
            "setup_actions": [],
            "validation_actions": [action.to_data()],
            "regression_cases": ["Check value"],
            "delivery_evidence": ["Passing check"],
            "blockers": [],
            "open_questions": [],
            "original_issue": None,
            "checkout": context["document"]["checkout"],
            "inspected_base_commit": self.workspace.base_revision,
            "resolved_base_branch": self.workspace.resolved_base_branch,
        }
        self.store.save_plan(
            document, context, reason="Initial plan", operation_id=str(uuid4())
        )
        self.snapshot = {"effective": {"setup_commands": [], "validation_commands": []}}
        self.allowed = True
        self.events = []
        self.stage = ImplementationStage(
            self.backend,
            self.journal,
            self.store,
            self.executor,
            self.sources,
            self.budget,
            self.snapshot,
            checkpoint=lambda: self.allowed,
            progress=self.events.append,
        )
        self.request = BackendRequest(
            {"backend": "fake", "credential_id": 1}, "test", "Implement plan", {}
        )

    def step(self):
        return self.stage.step(
            self.state, self.run_id, self.item, self.workspace, self.request
        )

    def patch(self, content="Changed value\n"):
        raw = (self.workspace.checkout / "README.md").read_bytes()
        return {
            "base_revision": self.workspace.base_revision,
            "content_digest": self.workspace.verify(),
            "files": [
                {
                    "path": "README.md",
                    "expected_digest": hashlib.sha256(raw).hexdigest(),
                    "content": content,
                }
            ],
        }

    def implement(self):
        self.backend.events = [
            lambda request: BackendEvent("patch", self.patch()),
            BackendEvent("complete", {"summary": "Done"}),
        ]
        self.assertEqual(self.step().detail["phase"], "implementation")
        self.assertEqual(self.step().detail["proposal"], "patch")
        self.assertEqual(self.step().detail["proposal"], "complete")
        self.assertEqual(self.step().detail["phase"], "validation")

    def ready(self):
        self.implement()
        self.broker.decision = "approved"
        self.assertEqual(self.step().detail["exit_code"], 0)
        return self.step()

    def test_manual_saved_plan_reaches_delivery_only_after_approved_validation(self):
        result = self.ready()
        self.assertEqual(result.status, "delivery_ready")
        self.assertEqual(result.detail["workspace"], workspace_evidence(self.workspace))
        self.assertEqual(len(result.detail["validation"]), 1)
        self.assertEqual(result.detail["validation"][0]["exit_code"], 0)
        self.assertNotIn("test output", str(result.detail))

    def test_zero_exit_model_without_validation_waits_for_approval(self):
        self.implement()
        result = self.step()
        self.assertEqual(result.status, "approval")
        self.assertEqual(self.executor.calls, [])
        self.assertEqual(self.backend.calls, 2)

    def test_human_wait_does_not_consume_active_budget(self):
        self.implement()
        self.step()
        spent = self.budget.data["total"]
        self.clock.now += 1000
        self.step()
        self.assertEqual(self.budget.data["total"], spent)

    def test_approval_polls_do_not_exhaust_saved_context_capacity(self):
        self.implement()
        self.step()
        refreshes = self.store.refreshes
        for _ in range(205):
            self.assertEqual(self.step().status, "approval")
        self.assertEqual(self.store.refreshes, refreshes)
        self.broker.decision = "approved"
        self.assertEqual(self.step().detail["exit_code"], 0)
        self.assertEqual(self.store.refreshes, refreshes + 1)

    def test_approved_pending_action_rechecks_fresh_discussion(self):
        self.implement()
        self.step()
        self.store.failure = BackendError("Discussion changed while waiting")
        self.assertEqual(self.step().status, "approval")
        self.broker.decision = "approved"
        with self.assertRaisesRegex(BackendError, "Discussion changed"):
            self.step()
        self.assertEqual(self.executor.calls, [])

    def test_no_change_is_explicit_failed_outcome(self):
        self.backend.events = [BackendEvent("complete", {"summary": "Already done"})]
        self.step()
        self.step()
        result = self.step()
        self.assertEqual(result.status, "failed")
        self.assertIn("no changes", result.detail["reason"])

    def test_change_after_passed_validation_invalidates_readiness(self):
        self.assertEqual(self.ready().status, "delivery_ready")
        (self.workspace.checkout / "README.md").write_text("Further change")
        self.backend.events = [BackendEvent("complete", {"summary": "Review again"})]
        self.assertEqual(self.step().status, "continue")
        self.step()
        self.broker.decision = "pending"
        self.assertEqual(self.step().status, "approval")
        self.assertEqual(len(self.executor.calls), 1)

    def test_failed_validation_repairs_with_remaining_budget(self):
        self.implement()
        self.broker.decision = "approved"
        self.executor.exit_codes = [1]
        self.assertEqual(self.step().detail["exit_code"], 1)
        self.backend.events = [
            lambda request: BackendEvent("patch", self.patch("Fixed\n")),
            BackendEvent("complete", {"summary": "Fixed"}),
        ]
        self.step()
        self.step()
        self.step()
        self.step()
        result = self.step()
        self.assertEqual(result.status, "delivery_ready")
        self.assertEqual(len(self.executor.calls), 2)
        self.assertEqual(self.budget.data["total"], 6)

    def test_validation_command_changing_files_requires_complete_rerun(self):
        self.implement()
        self.broker.decision = "approved"
        self.executor.change = lambda workspace: (
            workspace.checkout / "generated"
        ).write_text("created")
        self.assertEqual(self.step().detail["phase"], "review")
        self.step()
        self.step()
        self.assertEqual(self.step().status, "delivery_ready")
        self.assertEqual(len(self.executor.calls), 2)

    def test_configured_validation_cannot_be_omitted_by_plan(self):
        self.snapshot["effective"]["validation_commands"] = ["python3 -m unittest"]
        self.implement()
        self.broker.decision = "approved"
        self.step()
        self.assertEqual(self.step().detail["exit_code"], 0)
        self.assertEqual(
            self.executor.calls[-1][0].argv, ("/bin/sh", "-c", "python3 -m unittest")
        )
        self.assertEqual(self.step().status, "delivery_ready")

    def test_configured_setup_requires_separate_approval(self):
        self.snapshot["effective"]["setup_commands"] = ["python3 --version"]
        self.assertEqual(self.step().status, "approval")
        self.assertEqual(self.backend.calls, 0)
        self.broker.decision = "approved"
        self.assertEqual(self.step().detail["exit_code"], 0)
        self.assertEqual(self.step().detail["phase"], "implementation")

    def test_failed_configured_setup_requires_successful_retry_after_repair(self):
        self.snapshot["effective"]["setup_commands"] = ["required setup", "next setup"]
        self.broker.decision = "approved"
        self.executor.exit_codes = [1]
        self.assertEqual(self.step().detail["exit_code"], 1)
        self.backend.events = [
            lambda request: BackendEvent("patch", self.patch()),
            BackendEvent("complete", {"summary": "Repaired setup"}),
        ]
        self.step()
        self.assertEqual(self.step().detail["phase"], "setup")
        self.assertEqual(self.step().detail["exit_code"], 0)
        self.assertEqual(self.step().detail["exit_code"], 0)
        self.assertEqual(self.step().detail["phase"], "review")
        self.step()
        self.step()
        self.assertEqual(self.step().status, "delivery_ready")
        commands = [call[0].argv for call in self.executor.calls]
        self.assertEqual(
            commands[:3],
            [
                ("/bin/sh", "-c", "required setup"),
                ("/bin/sh", "-c", "required setup"),
                ("/bin/sh", "-c", "next setup"),
            ],
        )

    def test_explicit_revalidation_cannot_skip_failed_setup(self):
        self.snapshot["effective"]["setup_commands"] = ["required setup"]
        self.broker.decision = "approved"
        self.executor.exit_codes = [1]
        self.step()
        (self.workspace.checkout / "README.md").write_text("Manual repair\n")
        self.stage.revalidate(self.state, self.run_id)
        self.assertEqual(self.step().detail["exit_code"], 0)
        self.assertEqual(
            self.executor.calls[-1][0].argv, ("/bin/sh", "-c", "required setup")
        )

    def test_explicit_revalidation_cannot_skip_remaining_setup(self):
        self.snapshot["effective"]["setup_commands"] = ["first setup", "required setup"]
        self.broker.decision = "approved"
        self.step()
        with self.assertRaisesRegex(BackendError, "required setup"):
            self.stage.revalidate(self.state, self.run_id)
        self.assertEqual(self.step().detail["exit_code"], 0)
        self.assertEqual(
            self.executor.calls[-1][0].argv, ("/bin/sh", "-c", "required setup")
        )

    def test_changed_workspace_requires_new_approval(self):
        self.implement()
        self.step()
        old_action, old_operation = self.broker.requests[-1]
        (self.workspace.checkout / "README.md").write_text("Updated while waiting")
        self.step()
        action, operation = self.broker.requests[-1]
        self.assertNotEqual(action.binding_digest, old_action.binding_digest)
        self.assertNotEqual(operation, old_operation)

    def test_pause_stops_before_next_model_and_preserves_claim(self):
        self.step()
        self.allowed = False
        self.assertEqual(self.step().status, "wait")
        self.assertEqual(self.backend.calls, 0)
        self.assertEqual(self.budget.data["total"], 0)
        self.assertEqual(self.workspace.workspace_id, self.run_id)

    def test_uncertain_model_is_not_replayed(self):
        self.step()
        self.state["implementation"][self.run_id]["model_pending"] = True
        with self.assertRaisesRegex(BackendError, "uncertain"):
            self.step()
        self.assertEqual(self.backend.calls, 0)

    def test_uncertain_command_is_not_replayed(self):
        self.implement()
        self.step()
        self.state["implementation"][self.run_id]["pending"]["started"] = True
        self.broker.decision = "approved"
        with self.assertRaisesRegex(BackendError, "uncertain"):
            self.step()
        self.assertEqual(self.executor.calls, [])

    def test_stale_plan_handoff_blocks_model(self):
        self.store.failure = BackendError("Discussion changed; revised plan required")
        with self.assertRaisesRegex(BackendError, "Discussion changed"):
            self.step()
        self.assertEqual(self.backend.calls, 0)

    def test_material_amendment_is_saved_with_reason_and_invalidates_checks(self):
        self.step()
        document = copy.deepcopy(self.store.plan["document"])
        document["approach"] = "Use a safer approach"
        self.backend.events = [
            BackendEvent(
                "amendment",
                {"document": document, "reason": "New repository constraint"},
            )
        ]
        self.step()
        self.assertEqual(self.store.saved[-1]["reason"], "New repository constraint")
        self.backend.events = [BackendEvent("complete", {"summary": "Done"})]
        self.step()
        session = self.state["implementation"][self.run_id]
        self.assertEqual(
            session["amendments"][-1]["reason"], "New repository constraint"
        )
        self.assertEqual(session["checks"], [])

    def test_conflicting_patch_preserves_existing_work(self):
        patch = self.patch()
        patch["files"][0]["expected_digest"] = "a" * 64
        with self.assertRaisesRegex(BackendError, "conflicts"):
            with self.workspace.locked():
                apply_patch(self.workspace, patch)
        self.assertEqual(
            (self.workspace.checkout / "README.md").read_text(),
            "Untrusted repository instructions.\n",
        )

    def test_patch_git_metadata_is_forbidden(self):
        patch = self.patch()
        patch["files"][0]["path"] = ".git/config"
        with self.assertRaises(BackendError):
            apply_patch(self.workspace, patch)

    def test_patch_symlink_parent_is_forbidden(self):
        (self.workspace.checkout / "escape").symlink_to(
            self.root, target_is_directory=True
        )
        patch = self.patch()
        patch["files"][0].update(path="escape/file", expected_digest=None)
        with self.assertRaisesRegex(BackendError, "parent"):
            apply_patch(self.workspace, patch)
        self.assertFalse((self.root / "file").exists())

    def test_existing_dirty_branch_reused_without_reset(self):
        (self.workspace.checkout / "README.md").write_text(
            "Existing partial implementation"
        )
        workspace = self.manager.prepare(self.run_id, self.item)
        self.assertEqual(workspace.checkout, self.workspace.checkout)
        self.assertEqual(
            (workspace.checkout / "README.md").read_text(),
            "Existing partial implementation",
        )

    def test_wrong_branch_stops_without_checkout_or_reset(self):
        git(["checkout", "-b", "other"], self.workspace.checkout)
        with self.assertRaisesRegex(WorkspaceError, "branch changed"):
            self.step()
        self.assertEqual(
            git(["branch", "--show-current"], self.workspace.checkout), "other"
        )

    def test_conflicted_index_stops_before_model(self):
        file_hash = git(["hash-object", "README.md"], self.workspace.checkout)
        import subprocess
        from ..workspaces import clean_environment

        subprocess.run(
            ["/usr/bin/git", "update-index", "--index-info"],
            cwd=self.workspace.checkout,
            env=clean_environment(),
            input=f"0 {'0' * 40}\tconflicted\n100644 {file_hash} 1\tconflicted\n100644 {file_hash} 2\tconflicted\n".encode(),
            check=True,
        )
        with self.assertRaisesRegex(WorkspaceError, "conflicts"):
            self.step()
        self.assertEqual(self.backend.calls, 0)

    def test_task_budget_accumulates_research_and_validation(self):
        self.budget.run(
            "research",
            lambda remaining: setattr(self.clock, "now", self.clock.now + 49),
        )
        self.budget.run(
            "implementation",
            lambda remaining: setattr(self.clock, "now", self.clock.now + 48),
        )
        self.step()
        (self.workspace.checkout / "README.md").write_text("Prior implementation")
        self.executor.elapsed = 3
        self.backend.events = [BackendEvent("complete", {"summary": "Out of time"})]
        self.step()
        self.step()
        self.broker.decision = "approved"
        result = self.step()
        self.assertEqual(result.status, "failed")
        self.assertIn("timeout", result.detail["reason"])

    def test_actual_context_handoff_stops_pagination_at_remaining_research_budget(self):
        from unittest.mock import Mock, patch
        from ..context import GitHubContextClient
        from ..plan_store import ContextPlanStore
        from ..test_context import issue, comment

        self.item.update(
            source="github",
            issue_repository="https://github.com/original/issues.git",
            issue_number=7,
        )
        self.budget.run("research", lambda remaining: setattr(self.clock, "now", 49.0))
        client = Mock()
        self.stage.store = ContextPlanStore(
            client, self.run_id, str(uuid4()), self.sources
        )
        reader = GitHubContextClient("test-read-token")
        link = '<https://api.github.com/repos/original/issues/issues/7/comments?per_page=100&page=2>; rel="next"'
        pages = [(issue(comments=2), ""), ([comment()], link), ([comment(2)], "")]

        def page(*args, **kwargs):
            self.clock.now += 0.6
            return pages.pop(0)

        with patch("coding_runner.context.time.monotonic", side_effect=self.clock):
            with patch.object(reader, "_get", side_effect=page) as fetch:
                result = self.stage.step(
                    self.state,
                    self.run_id,
                    self.item,
                    self.workspace,
                    self.request,
                    issue_client=reader,
                )
        self.assertEqual(result.status, "failed")
        self.assertIn("timeout", result.detail["reason"])
        self.assertEqual(fetch.call_count, 2)
        client.request.assert_not_called()
        self.assertEqual(self.backend.calls, 0)
        self.assertAlmostEqual(self.budget.data["total"], 50.2)

    def test_gate_closure_during_context_save_stops_before_handoff(self):
        from unittest.mock import Mock
        from ..plan_store import ContextPlanStore

        client = Mock()

        def request(*args, **kwargs):
            self.allowed = False
            return {"context": self.store.context}

        client.request.side_effect = request
        self.stage.store = ContextPlanStore(
            client, self.run_id, str(uuid4()), self.sources
        )
        self.assertEqual(self.step().status, "wait")
        self.assertEqual(client.request.call_count, 1)
        self.assertTrue(client.request.call_args.args[1].endswith("/contexts/"))
        self.assertEqual(self.backend.calls, 0)

    def test_original_saved_context_read_consumes_research_budget(self):
        from unittest.mock import patch

        original = self.store.request

        def request(*args, **kwargs):
            self.clock.now += 0.5
            return original(*args, **kwargs)

        self.budget.run("research", lambda remaining: setattr(self.clock, "now", 49.7))
        with patch.object(self.store, "request", side_effect=request) as read:
            result = self.step()
        self.assertEqual(result.status, "failed")
        self.assertIn("timeout", result.detail["reason"])
        self.assertAlmostEqual(read.call_args.kwargs["timeout"], 0.3)
        self.assertAlmostEqual(self.budget.data["total"], 50.2)
        self.assertEqual(self.backend.calls, 0)

    def test_delivery_verification_can_share_supervisor_workspace_lock(self):
        result = self.ready()
        with self.workspace.locked():
            verified = verify_delivery_ready(
                self.workspace, result.detail, already_locked=True
            )
        self.assertEqual(verified["workspace"], result.detail["workspace"])

    def test_patch_creates_nested_files_without_running_commands(self):
        proposal = self.patch()
        proposal["files"] = [
            {"path": "new/module.py", "expected_digest": None, "content": "VALUE = 1\n"}
        ]
        with self.workspace.locked():
            apply_patch(self.workspace, proposal)
        self.assertEqual(
            (self.workspace.checkout / "new/module.py").read_text(), "VALUE = 1\n"
        )
        self.assertEqual(self.broker.consumed, [])

    def test_grouped_patch_checks_every_conflict_before_writing(self):
        proposal = self.patch()
        proposal["files"].append(
            {"path": "absent", "expected_digest": "a" * 64, "content": "new"}
        )
        with self.assertRaises(BackendError):
            with self.workspace.locked():
                apply_patch(self.workspace, proposal)
        self.assertEqual(
            (self.workspace.checkout / "README.md").read_text(),
            "Untrusted repository instructions.\n",
        )

    def test_supervised_commit_attests_unchanged_validated_files_without_repeating_checks(
        self,
    ):
        result = self.ready()
        git(["add", "README.md"], self.workspace.checkout)
        git(
            [
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.com",
                "commit",
                "-m",
                "Implement",
            ],
            self.workspace.checkout,
        )
        head = git(["rev-parse", "HEAD"], self.workspace.checkout)
        attestation = verify_delivery_ready(
            self.workspace, result.detail, committed_head=head
        )
        self.assertEqual(attestation["committed_head"], head)
        self.assertEqual(len(self.executor.calls), 1)
        with self.assertRaisesRegex(BackendError, "stale"):
            verify_delivery_ready(self.workspace, result.detail)

    def test_supervised_commit_preserves_new_file_diff_fingerprint(self):
        self.implement()
        (self.workspace.checkout / "new.py").write_text("VALUE = 1\n")
        self.stage.revalidate(self.state, self.run_id)
        self.step()
        self.broker.decision = "approved"
        self.step()
        result = self.step()
        git(["add", "README.md", "new.py"], self.workspace.checkout)
        git(
            [
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.com",
                "commit",
                "-m",
                "Create module",
            ],
            self.workspace.checkout,
        )
        head = git(["rev-parse", "HEAD"], self.workspace.checkout)
        attestation = verify_delivery_ready(
            self.workspace, result.detail, committed_head=head
        )
        self.assertEqual(
            attestation["workspace"]["diff_digest"],
            result.detail["workspace"]["diff_digest"],
        )
        self.assertEqual(len(self.executor.calls), 1)

    def test_commit_attestation_rejects_changed_validated_files(self):
        result = self.ready()
        (self.workspace.checkout / "README.md").write_text("Unvalidated content")
        git(["add", "README.md"], self.workspace.checkout)
        git(
            [
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.com",
                "commit",
                "-m",
                "Unvalidated",
            ],
            self.workspace.checkout,
        )
        head = git(["rev-parse", "HEAD"], self.workspace.checkout)
        with self.assertRaisesRegex(BackendError, "stale"):
            verify_delivery_ready(self.workspace, result.detail, committed_head=head)

    def test_commit_attestation_rejects_hidden_index_content(self):
        result = self.ready()
        path = self.workspace.checkout / "README.md"
        validated = path.read_bytes()
        path.write_text("Unchecked committed content\n")
        git(["add", "README.md"], self.workspace.checkout)
        path.write_bytes(validated)
        git(
            ["update-index", "--assume-unchanged", "README.md"], self.workspace.checkout
        )
        git(
            [
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.com",
                "commit",
                "-m",
                "Wrong blob",
            ],
            self.workspace.checkout,
        )
        self.assertEqual(git(["status", "--porcelain"], self.workspace.checkout), "")
        head = git(["rev-parse", "HEAD"], self.workspace.checkout)
        with self.assertRaisesRegex(BackendError, "tree"):
            verify_delivery_ready(self.workspace, result.detail, committed_head=head)

    def test_commit_attestation_rejects_git_attribute_byte_conversion(self):
        self.implement()
        (self.workspace.checkout / ".gitattributes").write_text("README.md text\n")
        (self.workspace.checkout / "README.md").write_bytes(b"Validated CRLF\r\n")
        self.stage.revalidate(self.state, self.run_id)
        self.step()
        self.broker.decision = "approved"
        self.step()
        result = self.step()
        git(["add", ".gitattributes", "README.md"], self.workspace.checkout)
        git(
            [
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.com",
                "commit",
                "-m",
                "Converted",
            ],
            self.workspace.checkout,
        )
        head = git(["rev-parse", "HEAD"], self.workspace.checkout)
        self.assertEqual(git(["status", "--porcelain"], self.workspace.checkout), "")
        with self.assertRaisesRegex(BackendError, "tree"):
            verify_delivery_ready(self.workspace, result.detail, committed_head=head)

    def test_explicit_revalidation_reviews_and_runs_checks_without_model(self):
        self.ready()
        calls = self.backend.calls
        (self.workspace.checkout / "README.md").write_text("New validation target")
        self.stage.revalidate(self.state, self.run_id)
        self.step()
        self.step()
        self.assertEqual(self.step().status, "delivery_ready")
        self.assertEqual(self.backend.calls, calls)

    def test_issue_task_uses_complete_refreshed_context(self):
        self.item.update(
            source="github",
            issue_repository="https://github.com/example/source",
            issue_number=7,
        )
        context = copy.deepcopy(self.store.context)
        document = context["document"]
        document.update(source="github", instructions="Current issue body")
        document["issue"] = {
            "number": 7,
            "repository": self.item["issue_repository"],
            "state": "open",
            "body": "Current issue body",
            "author": "author",
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
            "url": "https://github.com/example/source/issues/7",
        }
        document["comments"] = [
            {
                "id": 8,
                "body": "Current requirement",
                "author": "reviewer",
                "created_at": "2026-01-01T00:00:00Z",
                "updated_at": "2026-01-01T00:00:00Z",
                "url": "https://github.com/example/source/issues/7#issuecomment-8",
            }
        ]
        context["digest"] = digest(document)
        self.store.context = context
        self.store.origins[context["id"]] = context
        plan = copy.deepcopy(self.store.plan["document"])
        plan.update(
            context_digest=context["digest"], original_issue=document["issue"]["url"]
        )
        self.store.save_plan(
            plan,
            context,
            reason="Current discussion assessed",
            operation_id=str(uuid4()),
        )
        self.assertEqual(self.ready().status, "delivery_ready")
        self.assertGreaterEqual(self.store.refreshes, 6)

    def test_fresh_timestamp_keeps_unchanged_saved_plan(self):
        current = copy.deepcopy(self.store.context)
        current["id"] = str(uuid4())
        current["document"]["fetched_at"] = "2026-11-15T00:00:00Z"
        current["digest"] = digest(current["document"])
        self.store.context = current
        self.assertEqual(self.ready().status, "delivery_ready")

    def test_changed_discussion_blocks_even_with_stale_handoff_response(self):
        current = copy.deepcopy(self.store.context)
        current["document"]["instructions"] = "Changed manual instructions"
        current["digest"] = digest(current["document"])
        self.store.context = current
        with self.assertRaises(ValueError):
            self.step()
        self.assertEqual(self.backend.calls, 0)

    def test_saved_plan_without_material_reason_cannot_replace_active_plan(self):
        self.step()
        self.store.plan = {**self.store.plan, "id": str(uuid4()), "reason": ""}
        with self.assertRaisesRegex(BackendError, "reason"):
            self.step()

    def test_changed_frozen_backend_configuration_is_rejected(self):
        self.step()
        self.request = replace(self.request, model="different-model")
        with self.assertRaisesRegex(BackendError, "Frozen"):
            self.step()

    def test_actual_sandbox_stops_command_at_remaining_budget(self):
        self.implement()
        self.broker.decision = "approved"
        session = self.state["implementation"][self.run_id]
        action = copy.deepcopy(self.store.plan["document"]["validation_actions"][0])
        action["argv"] = ["/usr/bin/sleep", "10"]
        self.store.plan["document"]["validation_actions"] = [action]
        self.store.plan["digest"] = digest(self.store.plan["document"])
        session["plan_digest"] = self.store.plan["digest"]
        import time

        self.budget = ActiveBudget(
            self.journal,
            self.state,
            str(uuid4()),
            0.3,
            {"research": 0.3, "implementation": 0.3, "validation": 0.3},
        )
        self.stage.budget = self.budget
        self.stage.executor = ApprovedExecutor(
            self.broker, self.sources, lambda: True, periodic_gate=self.stage._periodic
        )
        started = time.monotonic()
        result = self.step()
        self.assertEqual(result.status, "failed")
        self.assertLess(time.monotonic() - started, 3)
        self.assertIn("timeout", result.detail["reason"])

    def test_real_sandbox_validation_records_actual_exit_code(self):
        self.implement()
        self.broker.decision = "approved"
        self.stage.executor = ApprovedExecutor(
            self.broker, self.sources, lambda: True, periodic_gate=self.stage._periodic
        )
        self.assertEqual(self.step().detail["exit_code"], 0)
        self.assertEqual(self.step().status, "delivery_ready")
        self.assertEqual(len(self.broker.consumed), 1)
