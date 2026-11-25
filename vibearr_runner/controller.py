import logging
import threading
from typing import Any
from uuid import uuid4

from .client import (
    APIError,
    AuthenticationError,
    Client,
    InvalidResponse,
)
from .journal import Journal, JournalError
from .gates import GateEvaluator
from .backend_registry import BackendRegistry
from .recovery import RecoveryCoordinator, ReconciliationRequired
from .lifecycle import Lifecycle

logger = logging.getLogger(__name__)


class ControllerStopped(Exception):
    pass


class OwnershipLost(ReconciliationRequired):
    pass


def processing_configuration(configuration):
    if configuration.get("native_dispatch_available") is True:
        return {**configuration, "execution_ready": True}
    return configuration


class Controller:
    def __init__(
        self,
        client: Client,
        journal: Journal,
        stop: threading.Event | None = None,
        gates: GateEvaluator | None = None,
        backends: BackendRegistry | None = None,
        lifecycle_factory=Lifecycle,
        maintenance=None,
        completed=None,
    ):
        self.client = client
        self.journal = journal
        self.gates = gates if gates is not None else GateEvaluator(journal)
        self.stop = stop if stop is not None else threading.Event()
        self.backends = backends if backends is not None else BackendRegistry()
        self.maintenance, self.completed = maintenance, completed
        self.lifecycle_factory = lifecycle_factory
        self.state: dict[str, Any] = {}

    def probe(self, state: dict[str, Any]) -> dict[str, Any]:
        self.check_shutdown()
        identity = self.client.identity()
        runner = identity.get("runner", {})
        runner_id = runner.get("id") if isinstance(runner, dict) else None
        if identity.get("schema_version") != 1 or type(runner_id) is not int:
            raise InvalidResponse("Runner identity response is invalid.")
        if state.get("runner_id") not in {None, runner_id}:
            raise JournalError("Journal belongs to a different runner identity.")
        self.check_shutdown()
        configuration = self.client.configuration()
        if configuration.get("schema_version") != 1:
            raise InvalidResponse("Runner configuration version is unsupported.")
        revision = configuration.get("revision")
        if not isinstance(revision, str) or not revision:
            raise InvalidResponse("Runner configuration revision is missing.")
        if not isinstance(configuration.get("global"), dict):
            raise InvalidResponse("Runner global configuration is missing.")
        self.check_shutdown()
        active = self.client.active()
        if (
            "run" not in active
            or active["run"] is not None
            and not isinstance(active["run"], dict)
        ):
            raise InvalidResponse("Active-run response is invalid.")
        active_id = None if active["run"] is None else active["run"].get("id")
        if active["run"] is not None and not isinstance(active_id, str):
            raise InvalidResponse("Active-run identity is invalid.")
        state["remote_active_run_id"] = active_id
        state["runner_id"] = runner_id
        self.journal.save(state)
        return configuration

    def checkpoint(self) -> bool:
        if self.stop.is_set():
            return False
        state = self.state
        run_id = state["run"]["run"]["id"]
        remote = self.client.reconcile(run_id)
        if remote["run"]["state"] != "active":
            raise OwnershipLost("Remote run no longer owns execution.")
        state["run"]["run"] = remote["run"]
        if remote["run"].get("cancellation_requested_at"):
            self.journal.save(state)
            return False
        self.check_shutdown()
        configuration = self.client.configuration()
        self.check_shutdown()
        self.gates.adapters.update(
            self.backends.usage_adapters(configuration["accounts"])
        )
        decision = self.gates.evaluate(
            processing_configuration(configuration),
            state,
            checkpoint="stage",
            run_id=run_id,
        )
        self.check_shutdown()
        self.client.gate_report(decision.data())
        self.check_shutdown()
        return decision.allowed

    def reconcile(self, state: dict[str, Any]) -> None:
        self.check_shutdown()
        RecoveryCoordinator(
            self.client, self.journal, self.gates, check_shutdown=self.check_shutdown
        ).reconcile(state)

    def cycle(self, state, configuration):
        self.check_shutdown()
        self.state = state
        recovery = RecoveryCoordinator(
            self.client, self.journal, self.gates, check_shutdown=self.check_shutdown
        )
        recovery.bind(state)
        self.check_shutdown()
        recovery.reconcile(state)
        self.check_shutdown()
        lifecycle = self.lifecycle_factory(
            self.client, self.journal, self.checkpoint, stop=self.stop
        )
        local = state.get("run")
        if local:
            if local["run"]["state"] == "completed":
                if self.completed:
                    self.completed(
                        state,
                        local["run"]["id"],
                        local["item"].get("claim_token"),
                        local["run"].get("finish_digest"),
                    )
                for field in (
                    "run",
                    "context",
                    "saved_plan",
                    "delivery",
                    "delivery_ready",
                    "failure",
                    "cancellation",
                    "stage",
                    "wait_reason",
                    "request",
                    "native",
                ):
                    state.pop(field, None)
                self.journal.save(state)
                return "completed"
            return lifecycle.step(state)
        self.check_shutdown()
        if self.maintenance and self.maintenance(state, configuration) is not True:
            return "wait"
        from .drain import acknowledge

        if acknowledge(self.journal, state):
            return "drained"
        self.check_shutdown()
        self.gates.adapters.update(
            self.backends.usage_adapters(configuration["accounts"])
        )
        decision = self.gates.evaluate(
            processing_configuration(configuration), state, checkpoint="claim"
        )
        self.check_shutdown()
        self.client.gate_report(decision.data())
        self.check_shutdown()
        native = isinstance(lifecycle, Lifecycle)
        ready = (
            lifecycle.ready(configuration, state)
            if native and decision.allowed
            else None
        )
        if not decision.allowed or not (
            ready if native else lifecycle.ready(configuration)
        ):
            if not decision.allowed:
                self.suspend_owned(state)
            elif native and self.maintenance and state.get("native_readiness_error"):
                self.maintenance.blocked(state, state["native_readiness_error"])
            return "wait"
        self.check_shutdown()
        run_id = str(uuid4())
        self.gates.prepare_claim(state, run_id, decision)
        payload = {
            "run_id": run_id,
            "configuration_revision": configuration["revision"],
        }
        if native:
            payload["project_ids"] = lifecycle.claim_project_ids
        self.journal.prepare(state, run_id, "claim", payload)
        self.check_shutdown()
        result = self.client.request("POST", "/api/v1/runner/runs/claim/", payload)
        recovery.accept_claim(state, result)
        return "claimed" if state.get("run") else "empty"

    def run(self, check: bool = False) -> None:
        state = self.journal.load()
        if state.get("controller_secret"):
            self.client.controller_secret = state["controller_secret"]
        self.state = state
        delay = 1
        while not self.stop.is_set():
            try:
                configuration = self.probe(state)
                self.check_shutdown()
                if check:
                    operation = state.get("operation")
                    if operation and operation.get("status") == "pending":
                        operation["reconciliation"] = self.client.reconcile(
                            operation["run_id"]
                        )
                        self.journal.save(state)
                    logger.info(
                        "Authenticated startup check passed; no work was claimed."
                    )
                    return
                result = self.cycle(state, configuration)
                poll = configuration["global"].get("polling_interval_seconds", 30)
                if type(poll) is not int or not 1 <= poll <= 86400:
                    raise InvalidResponse("Polling interval is invalid.")
                delay = 1
                self.stop.wait(
                    0.01
                    if result
                    in {"continue", "claimed", "saved", "delivery_ready", "completed"}
                    else poll
                )
            except AuthenticationError:
                self.suspend_owned(state, ownership_lost=True)
                raise
            except ControllerStopped:
                return
            except ReconciliationRequired as exc:
                self.suspend_owned(state, ownership_lost=isinstance(exc, OwnershipLost))
                state["reconciliation_required"] = str(exc)
                self.journal.save(state)
                logger.warning("Execution blocked: %s", exc)
                if self.maintenance and hasattr(self.maintenance, "blocked"):
                    try:
                        self.maintenance.blocked(state, str(exc))
                    except APIError:
                        logger.warning(
                            "Recovery status could not be reported; local journal remains authoritative."
                        )
                self.stop.wait(30)
            except APIError as exc:
                self.suspend_owned(state)
                if check:
                    raise
                logger.warning(
                    "Queue operation failed (%s); retrying after reconciliation.",
                    type(exc).__name__,
                )
                self.stop.wait(delay)
                delay = min(delay * 2, 60)
            except JournalError:
                self.suspend_owned(state, ownership_lost=True)
                raise

    def suspend_owned(self, state, *, ownership_lost=False):
        native = state.get("native") or state.get("native_preflight")
        if not native or self.stop.is_set():
            return
        try:
            lifecycle = self.lifecycle_factory(
                self.client,
                self.journal,
                self.checkpoint,
                stop=self.stop,
            )
            if hasattr(lifecycle, "suspend_owned"):
                lifecycle.suspend_owned(state, ownership_lost=ownership_lost)
        except Exception as exc:
            state["native_stop_uncertain"] = str(exc)[:500]
            self.journal.save(state)
            logger.warning(
                "Owned native operations remain fenced: %s", type(exc).__name__
            )

    def check_shutdown(self) -> None:
        if self.stop.is_set():
            raise ControllerStopped
