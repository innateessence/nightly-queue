import threading
import time
from uuid import uuid4

from .approvals import ActionSpec, ApprovalBroker, ApprovalRequired
from .backend_registry import BackendRegistry, ScopedCredentials
from .backends import BackendError, BackendInterrupted
from .budgets import ActiveBudget, BudgetExceeded
from .context import ContextError, ContextInterrupted, GitHubContextClient
from .implementation import ImplementationStage
from .journal import Journal
from .processes import ProcessTracker, current_execution, current_tracker
from .recovery import DurableClient, DurableContextPlanStore, ReconciliationRequired
from .research import ResearchStage
from .sandbox import ApprovedExecutor
from .workspaces import SourceStore, WorkspaceError, WorkspaceManager


class Lifecycle:
    def __init__(
        self,
        client,
        journal: Journal,
        backends: BackendRegistry,
        checkpoint,
        *,
        stop=None,
        workspace_manager=None,
        research_factory=ResearchStage,
        implementation_factory=ImplementationStage,
        delivery_factory=None,
        executor_factory=ApprovedExecutor,
    ):
        self.client, self.journal, self.backends = client, journal, backends
        self.checkpoint, self.stop = checkpoint, stop or threading.Event()
        self.workspaces = workspace_manager or WorkspaceManager(
            journal.directory / "workspaces"
        )
        self.research_factory, self.implementation_factory = (
            research_factory,
            implementation_factory,
        )
        self.delivery_factory, self.executor_factory = (
            delivery_factory,
            executor_factory,
        )

    def ready(self, configuration):
        # Native runtime/adapter qualification replaces the retired proposal path.
        return False

    def secret(self, run_id, claim_token, purpose, **limits):
        result = self.client.request(
            "POST",
            "/api/v1/runner/secrets/",
            {
                "scope": "run",
                "run_id": run_id,
                "claim_token": claim_token,
                "purpose": purpose,
            },
            **limits,
        )
        token = result.get("material", {}).get("secret")
        if not isinstance(token, str) or not token:
            raise BackendError("Selected run credential is unavailable.")
        return token

    def progress(self, state, stage, detail=None, wait_reason=""):
        run = state["run"]
        run_id = run["run"]["id"]
        self.client.request(
            "POST",
            f"/api/v1/runner/runs/{run_id}/progress/",
            {
                "operation_id": str(uuid4()),
                "claim_token": run["item"]["claim_token"],
                "stage": stage,
                "wait_reason": wait_reason,
                "data": detail or {},
            },
        )
        state["stage"], state["wait_reason"] = stage, wait_reason
        self.journal.save(state)

    def fail(self, state, reason):
        run_id = state["run"]["run"]["id"]
        state["failure"] = reason
        self.journal.save(state)
        self.client.request(
            "POST",
            f"/api/v1/runner/runs/{run_id}/finish/",
            {
                "claim_token": state["run"]["item"]["claim_token"],
                "status": "failed",
                "result_url": (state.get("delivery", {}).get("pull_request") or {}).get(
                    "url", ""
                ),
                "error_summary": reason,
            },
        )
        return "reporting_pending"

    def cancel(self, state):
        ProcessTracker(self.journal, state).recover()
        run = state["run"]
        run_id, token = run["run"]["id"], run["item"]["claim_token"]
        path = f"/api/v1/runner/runs/{run_id}/cancellation/"
        self.client.request(
            "POST",
            path,
            {
                "claim_token": token,
                "reason": run["run"].get("cancellation_reason", ""),
                "termination_confirmed": True,
            },
        )
        state["cancellation"] = {"termination_confirmed": True}
        self.journal.save(state)
        return self.fail(
            state, "Cancelled after confirmed backend and child termination."
        )

    def step(self, state):
        if self.stop.is_set():
            return "wait"
        run = state["run"]
        if run["run"].get("cancellation_requested_at"):
            return self.cancel(state)
        delivery = state.get("delivery") or {}
        if delivery.get("phase") in {"delivered", "reporting_pending", "finished"}:
            from .delivery import DeliveryStage

            return DeliveryStage(self.journal, self.client, None).report(
                state, run["run"]["id"], run["item"]["claim_token"]
            )["status"]
        if state.get("failure"):
            return self.fail(state, state["failure"])
        if (
            run["run"].get("configuration_snapshot", {}).get("execution_kind")
            == "native"
        ):
            raise ReconciliationRequired(
                "Native task dispatch is not installed; legacy proposals cannot execute native work."
            )
        if not self.checkpoint():
            return "wait"
        context_token = current_tracker.set(ProcessTracker(self.journal, state))
        try:
            result = self._step(state, run["run"]["id"])
            if (state.get("request") or {}).get("status") == "received":
                state["request"] = None
                self.journal.save(state)
            return result
        except ApprovalRequired as exc:
            self.progress(state, "waiting", wait_reason=str(exc))
            return "approval"
        except (BackendInterrupted, ContextInterrupted):
            return "wait"
        except (BackendError, BudgetExceeded, WorkspaceError, ContextError) as exc:
            raise ReconciliationRequired(str(exc)) from None
        finally:
            current_tracker.reset(context_token)

    def waiting_for_approval(self, state, client, run_id, token):
        stage = "implementation" if state.get("saved_plan") else "research"
        pending = state.get(stage, {}).get(run_id, {}).get("pending") or {}
        if (
            pending.get("kind") != "action"
            or pending.get("started")
            or pending.get("execution_started")
        ):
            return False
        operation = state.get("request")
        path = f"/api/v1/runner/runs/{run_id}/approvals/"
        if operation and operation["path"] != path:
            return False
        action = ActionSpec.from_data(pending["data"]["action"])
        broker = ApprovalBroker(client, run_id, token)
        approval = broker.request(action, pending["operation_id"])
        if approval["decision"] == "approved":
            return False
        self.progress(
            state,
            "waiting",
            {"approval_id": approval["id"], "decision": approval["decision"]},
            "approval",
        )
        return True

    def _step(self, state, run_id):
        run = state["run"]
        item, snapshot = run["item"], run["run"]["configuration_snapshot"]
        token = item["claim_token"]
        client = DurableClient(self.client, self.journal, state)
        if self.waiting_for_approval(state, client, run_id, token):
            return "approval"
        controls = snapshot["global"]
        limits = {
            stage: controls[f"{stage}_timeout_seconds"]
            for stage in ("research", "implementation", "validation")
        }
        budget = ActiveBudget(
            self.journal, state, run_id, controls["task_timeout_seconds"], limits
        )

        def local_checkpoint():
            return not self.stop.is_set() and budget.checkpoint()

        def checkpoint():
            return local_checkpoint() and self.checkpoint() is True

        if (state.get("request") or {}).get("status") == "pending":
            budget.run(
                None,
                lambda remaining: client.recover(
                    timeout=remaining, checkpoint=local_checkpoint
                ),
            )

        def prepare(remaining):
            execution = current_execution.set(
                (time.monotonic() + remaining, local_checkpoint, checkpoint)
            )
            try:
                if isinstance(self.workspaces, WorkspaceManager):
                    from .delivery import SupervisorGit

                    if not checkpoint():
                        raise BackendInterrupted(
                            "Workspace preparation is waiting at a control checkpoint."
                        )
                    repository_token = self.secret(
                        run_id,
                        token,
                        "repository",
                        timeout=budget.remaining(None),
                        checkpoint=local_checkpoint,
                    )
                    repository_git = SupervisorGit(token=repository_token)
                    self.workspaces.remote_git = (
                        lambda arguments, cwd=None: repository_git.command(
                            arguments,
                            item["repository_url"],
                            self.journal.directory,
                            cwd,
                        )
                    )
                workspace = self.workspaces.prepare(run_id, item)
                if hasattr(self.workspaces, "inspect_base"):
                    self.workspaces.inspect_base(workspace)
                return workspace
            finally:
                current_execution.reset(execution)

        workspace = budget.run(None, prepare)
        sources = SourceStore(workspace.directory / "sources")
        store = DurableContextPlanStore(client, run_id, token, sources)
        delivery = state.get("delivery")
        if delivery or state.get("delivery_ready"):
            if self.delivery_factory:
                stage = self.delivery_factory(client, self.checkpoint)
            else:
                from .delivery import DeliveryStage, GitHubDeliveryClient, SupervisorGit

                secret = self.secret(run_id, token, "repository")
                stage = DeliveryStage(
                    self.journal,
                    client,
                    GitHubDeliveryClient(secret),
                    repository_git=SupervisorGit(token=secret),
                    gate=self.checkpoint,
                )
            evidence = state["delivery_ready"]
            plan = state["saved_plan"]["document"]
            result = stage.step(state, run_id, workspace, evidence, plan, token)
            if result["status"] == "failed":
                return self.fail(state, result["error"])
            return result["status"]
        issue = None
        if item["source"] != "manual":
            issue_token = budget.run(
                "research",
                lambda remaining: self.secret(
                    run_id,
                    token,
                    "issue",
                    timeout=remaining,
                    checkpoint=local_checkpoint,
                ),
            )
            issue = GitHubContextClient(issue_token)
        broker = ApprovalBroker(client, run_id, token)
        if not state.get("context"):
            self.progress(state, "context")
            try:
                saved = budget.run(
                    "research",
                    lambda remaining: store.refresh(
                        item,
                        workspace.base_revision,
                        issue_client=issue,
                        resolved_base_branch=workspace.resolved_base_branch,
                        deadline=time.monotonic() + remaining,
                        checkpoint=checkpoint,
                        inflight_checkpoint=local_checkpoint,
                    ),
                )
            except ContextInterrupted:
                if budget.remaining("research") <= 0:
                    raise BudgetExceeded(
                        "Initial context exhausted its active research budget."
                    ) from None
                raise
            state["context"] = saved
            self.journal.save(state)
            return "continue"
        stage_name = "implementation" if state.get("saved_plan") else "research"
        credentials = ScopedCredentials(
            self.client, run_id=run_id, claim_token=token, stage=stage_name
        )
        backend, request = self.backends.stage(snapshot, stage_name, credentials)
        executor = self.executor_factory(
            broker, sources, self.checkpoint, read_only=stage_name == "research"
        )

        if isinstance(executor, ApprovedExecutor):
            from .run_logs import RunLogs

            def publish_log(summary):
                client.request(
                    "POST",
                    f"/api/v1/runner/runs/{run_id}/artifacts/",
                    {
                        "operation_id": summary["execution_id"],
                        "claim_token": token,
                        "kind": "execution_log",
                        "data": summary,
                    },
                )

            executor.observer = RunLogs(self.journal.directory / "logs", publish_log)

        def progress(event):
            self.progress(state, stage_name, {"event": event.kind, **event.data})

        self.progress(state, stage_name)
        if stage_name == "research":
            stage = self.research_factory(
                backend,
                self.journal,
                store,
                executor,
                sources,
                checkpoint=self.checkpoint,
                progress=progress,
                stop=self.stop,
                budget=budget,
            )
            result = stage.step(
                state, run_id, item, state["context"], workspace, request
            )
            if result.status == "saved":
                state["saved_plan"] = result.detail
        else:
            stage = self.implementation_factory(
                backend,
                self.journal,
                store,
                executor,
                sources,
                budget,
                snapshot,
                checkpoint=self.checkpoint,
                progress=progress,
                stop=self.stop,
            )
            result = stage.step(
                state, run_id, item, workspace, request, issue_client=issue
            )
            if result.status == "delivery_ready":
                state["delivery_ready"] = result.detail
            if result.status == "failed":
                return self.fail(state, result.detail["reason"])
        state["wait_reason"] = (
            result.status if result.status in {"approval", "wait"} else ""
        )
        self.journal.save(state)
        if state["wait_reason"]:
            self.progress(state, "waiting", result.detail, result.status)
        return result.status
