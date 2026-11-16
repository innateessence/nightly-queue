import secrets
from typing import Any
from uuid import UUID

from .client import NotFoundError
from .journal import Journal, JournalError
from .processes import ProcessTracker


class ReconciliationRequired(JournalError):
    pass


class DurableClient:
    def __init__(self, client, journal: Journal, state: dict[str, Any]):
        self.client, self.journal, self.state = client, journal, state

    def __getattr__(self, name):
        return getattr(self.client, name)

    def recover(self):
        operation = self.state.get("request")
        if not operation or operation.get("status") == "received":
            return
        body = operation["payload"]
        run_id = operation["path"].split("/")[5]
        lookup = f"/api/v1/runner/runs/{run_id}/operations/{body['operation_id']}/"
        try:
            result = self.client.request("GET", lookup)["response"]
        except NotFoundError:
            result = self.client.request("POST", operation["path"], body)
        operation.update({"status": "received", "response": result})
        self.journal.save(self.state)

    def request(self, method, path, payload=None):
        endpoint = path.rstrip("/").split("/")[-1]
        durable = endpoint in {
            "contexts",
            "plans",
            "handoff",
            "progress",
            "artifacts",
            "approvals",
        }
        if method != "POST" or not durable:
            return self.client.request(method, path, payload)
        previous = self.state.get("request")
        if previous:
            same_id = previous["payload"].get("operation_id") == (payload or {}).get(
                "operation_id"
            )
            if same_id:
                if previous["path"] != path or previous["payload"] != payload:
                    raise ReconciliationRequired(
                        "Operation ID was reused with different request content."
                    )
                self.recover()
                return previous["response"]
            if previous["status"] != "received":
                self.recover()
                raise ReconciliationRequired(
                    "Recovered the previous metadata operation; acknowledge it before another mutation."
                )
            self.state["request"] = None
        if not isinstance(payload, dict) or "operation_id" not in payload:
            raise ReconciliationRequired(
                "Metadata mutation needs a durable operation ID."
            )
        self.state["request"] = {"path": path, "payload": payload, "status": "pending"}
        self.journal.save(self.state)
        result = self.client.request(method, path, payload)
        self.state["request"].update({"status": "received", "response": result})
        self.journal.save(self.state)
        return result


class RecoveryCoordinator:
    def __init__(self, client, journal, gates):
        self.client, self.journal, self.gates = client, journal, gates

    def bind(self, state):
        secret = state.get("controller_secret")
        if secret is None:
            if state.get("remote_active_run_id") or state.get("run"):
                raise ReconciliationRequired(
                    "Owned work has no local volume proof; operator recovery is required."
                )
            secret = secrets.token_hex(32)
            state["controller_secret"] = secret
            self.journal.save(state)
        if not isinstance(secret, str) or len(secret) != 64:
            raise ReconciliationRequired("Invalid private controller proof.")
        self.client.controller_secret = secret
        self.client.request("POST", "/api/v1/runner/controller/", {})

    def reconcile(self, state):
        ProcessTracker(self.journal, state).recover()
        operation = state.get("operation")
        local = state.get("run")
        remote_id = state.get("remote_active_run_id")
        pending_claim = state.get("gates", {}).get("pending_claim")
        if pending_claim and not operation and not local:
            pending_id = pending_claim["run_id"]
            try:
                result = self.client.reconcile(pending_id)
            except NotFoundError:
                if remote_id:
                    raise ReconciliationRequired(
                        "Unknown remote claim conflicts with original accounting."
                    ) from None
                self.gates.abandon_claim(state, pending_id)
            else:
                self.accept_claim(state, result)
                local = state.get("run")
        if operation and operation["kind"] == "claim":
            run_id = str(UUID(operation["run_id"]))
            try:
                result = self.client.reconcile(run_id)
            except NotFoundError:
                if remote_id:
                    raise ReconciliationRequired(
                        "Remote owned work conflicts with the pending claim."
                    ) from None
                from .backend_registry import BackendRegistry, ScopedCredentials

                configuration = self.client.configuration()
                accounts = (
                    [*configuration["accounts"], *pending_claim.get("accounts", [])]
                    if pending_claim
                    else configuration["accounts"]
                )
                adapters = BackendRegistry().usage_adapters(
                    accounts, ScopedCredentials(self.client)
                )
                for key, adapter in adapters.items():
                    self.gates.adapters.setdefault(key, adapter)
                decision = self.gates.evaluate(
                    configuration, state, checkpoint="claim", retry_claim_id=run_id
                )
                self.client.gate_report(decision.data())
                if not decision.allowed:
                    raise ReconciliationRequired(
                        "Pending claim retry is blocked by current processing gates."
                    ) from None
                result = self.client.request(
                    "POST", "/api/v1/runner/runs/claim/", operation["payload"]
                )
            self.accept_claim(state, result)
            local = state.get("run")
        if remote_id and (not local or local["run"]["id"] != remote_id):
            raise ReconciliationRequired(
                "Remote owned work has no matching local journal; operator recovery is required."
            )
        if not local:
            return None
        run_id = local["run"]["id"]
        remote = self.client.reconcile(run_id)
        if remote["run"]["state"] == "obsolete":
            state["reconciliation_required"] = (
                "Ownership is obsolete; execution is fenced."
            )
            self.journal.save(state)
            raise ReconciliationRequired(state["reconciliation_required"])
        if remote["run"]["state"] == "completed":
            if remote.get("item") and local.get("item", {}).get("claim_token"):
                remote["item"]["claim_token"] = local["item"]["claim_token"]
            state["run"] = remote
            state["remote_active_run_id"] = None
            if state.get("gates", {}).get("owned_run_id") == run_id:
                self.gates.complete_run(state, run_id)
            self.journal.save(state)
            return remote
        if remote["run"]["state"] != "active":
            raise ReconciliationRequired("Unexpected remote run state.")
        token = local.get("item", {}).get("claim_token")
        if not token:
            raise ReconciliationRequired(
                "Local claim proof is missing; do not recover a token from the server."
            )
        remote["item"]["claim_token"] = token
        state["run"] = remote
        state["remote_active_run_id"] = run_id
        for stage in ("research", "implementation"):
            session = state.get(stage, {}).get(run_id, {})
            pending = session.get("pending") or {}
            uncertain = (
                session.get("model_pending")
                or pending.get("started")
                or pending.get("execution_started")
            )
            if uncertain and not remote["run"].get("cancellation_requested_at"):
                raise ReconciliationRequired(
                    "Previous model or workspace operation has an uncertain outcome; operator reconciliation is required."
                )
        budget = state.get("budgets", {}).get(run_id, {})
        if budget.get("active") and not remote["run"].get("cancellation_requested_at"):
            raise ReconciliationRequired(
                "Interrupted active time needs conservative operator reconciliation."
            )
        self.journal.save(state)
        return remote

    def accept_claim(self, state, result):
        run = result["run"]
        run_id = run["id"]
        if run["state"] == "active":
            if not result.get("item", {}).get("claim_token"):
                result = self.client.request(
                    "POST", f"/api/v1/runner/runs/{run_id}/claim/", {}
                )
            state["run"] = result
            state["remote_active_run_id"] = run_id
            self.gates.record_claim(state, run_id)
        elif run["state"] == "empty":
            pending = state["gates"].get("pending_claim")
            if pending and pending["run_id"] == run_id:
                state["operation"] = None
                self.gates.abandon_claim(state, run_id)
            elif operation := state.get("operation"):
                if operation["run_id"] != run_id:
                    raise ReconciliationRequired(
                        "Empty claim accounting does not match."
                    )
        else:
            raise ReconciliationRequired(
                "Claim result is neither owned work nor empty."
            )
        state["operation"] = None
        self.journal.save(state)


from .plan_store import ContextPlanStore  # noqa: E402
from .context import digest  # noqa: E402
from .plans import register_context_sources  # noqa: E402


class DurableContextPlanStore(ContextPlanStore):
    def refresh(self, item, base_commit, **kwargs):
        pending = self.client.state.get("request")
        path = f"/api/v1/runner/runs/{self.run_id}/contexts/"
        if not pending or pending["path"] != path:
            return super().refresh(item, base_commit, **kwargs)
        payload = pending["payload"]
        if payload["base_commit"] != base_commit:
            raise ReconciliationRequired(
                "Pending context names a different inspected base."
            )
        saved = self.client.request("POST", path, payload)["context"]
        if saved["digest"] != digest(saved["document"]):
            raise ReconciliationRequired("Recovered context digest did not match.")
        saved["sources"] = register_context_sources(saved["document"], self.sources)
        return saved

    def handoff(self, item, base_commit, **kwargs):
        pending = self.client.state.get("request")
        path = f"/api/v1/runner/runs/{self.run_id}/handoff/"
        if pending and pending["path"] == path:
            payload = pending["payload"]
            if payload["base_commit"] != base_commit:
                raise ReconciliationRequired(
                    "Pending handoff names a different inspected base."
                )
            result = self.client.request("POST", path, payload)
            register_context_sources(result["context"]["document"], self.sources)
            return result
        return super().handoff(item, base_commit, **kwargs)
