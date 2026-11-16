import threading
from uuid import uuid4

from .approvals import ApprovalBroker, ApprovalRequired
from .backend_registry import BackendRegistry, ScopedCredentials
from .backends import BackendError, BackendInterrupted
from .budgets import ActiveBudget, BudgetExceeded
from .context import GitHubContextClient
from .implementation import ImplementationStage
from .journal import Journal
from .processes import ProcessTracker, current_tracker
from .recovery import DurableClient, DurableContextPlanStore, ReconciliationRequired
from .research import ResearchStage
from .sandbox import ApprovedExecutor, probe_sandbox
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
        if configuration.get("execution_ready") is not True:
            return False
        for account in configuration["accounts"]:
            backend = self.backends.create(
                account["backend"], ScopedCredentials(self.client)
            )
            readiness = backend.readiness(account)
            caps = readiness.capabilities
            valid = (
                readiness.ready
                and readiness.authenticated
                and caps.research
                and caps.implementation
            )
            if (
                not valid
                or caps.native_tools
                or caps.transport != "structured_proposals"
                or not caps.graceful_interrupt
            ):
                return False
        if not configuration["accounts"] or any(
            project["errors"] for project in configuration["projects"]
        ):
            return False
        probe = self.journal.directory / "sandbox-probe"
        probe.mkdir(mode=0o700, exist_ok=True)
        probe_sandbox(probe)
        return True

    def secret(self, run_id, claim_token, purpose):
        result = self.client.request(
            "POST",
            "/api/v1/runner/secrets/",
            {
                "scope": "run",
                "run_id": run_id,
                "claim_token": claim_token,
                "purpose": purpose,
            },
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
        except BackendInterrupted:
            return "wait"
        except (BackendError, BudgetExceeded, WorkspaceError) as exc:
            raise ReconciliationRequired(str(exc)) from None
        finally:
            current_tracker.reset(context_token)

    def _step(self, state, run_id):
        run = state["run"]
        item, snapshot = run["item"], run["run"]["configuration_snapshot"]
        token = item["claim_token"]
        client = DurableClient(self.client, self.journal, state)
        client.recover()
        if isinstance(self.workspaces, WorkspaceManager):
            from .delivery import SupervisorGit

            repository_token = self.secret(run_id, token, "repository")
            repository_git = SupervisorGit(token=repository_token)
            self.workspaces.remote_git = (
                lambda arguments, cwd=None: repository_git.command(
                    arguments, item["repository_url"], self.journal.directory, cwd
                )
            )
        workspace = self.workspaces.prepare(run_id, item)
        if hasattr(self.workspaces, "inspect_base"):
            self.workspaces.inspect_base(workspace)
        sources = SourceStore(workspace.directory / "sources")
        store = DurableContextPlanStore(client, run_id, token, sources)
        controls = snapshot["global"]
        limits = {
            stage: controls[f"{stage}_timeout_seconds"]
            for stage in ("research", "implementation", "validation")
        }
        budget = ActiveBudget(
            self.journal, state, run_id, controls["task_timeout_seconds"], limits
        )
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
        issue = (
            GitHubContextClient(self.secret(run_id, token, "issue"))
            if item["source"] != "manual"
            else None
        )
        broker = ApprovalBroker(client, run_id, token)
        if not state.get("context"):
            self.progress(state, "context")
            saved = store.refresh(
                item,
                workspace.base_revision,
                issue_client=issue,
                resolved_base_branch=workspace.resolved_base_branch,
            )
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
