import threading

from .journal import Journal
from .recovery import ReconciliationRequired
from .workspaces import WorkspaceManager


class Lifecycle:
    def __init__(
        self, client, journal: Journal, checkpoint, *, stop=None, workspace_manager=None
    ):
        self.client, self.journal = client, journal
        self.checkpoint, self.stop = checkpoint, stop or threading.Event()
        self.workspaces = workspace_manager or WorkspaceManager(
            journal.directory / "workspaces"
        )

    def native_engine(self):
        if not hasattr(self, "_native_engine"):
            from .native_lifecycle import NativeLifecycle

            self._native_engine = NativeLifecycle(self)
        return self._native_engine

    def ready(self, configuration, state=None):
        if state is None:
            return False
        try:
            engine = self.native_engine()
            allowed = engine.ready(configuration, state)
            self.claim_project_ids = engine.ready_projects
            return allowed
        except (ImportError, OSError, ValueError) as exc:
            state["native_readiness_error"] = str(exc)[:500]
            self.journal.save(state)
            return False

    def suspend_owned(self, state, *, ownership_lost=False):
        from .native_resources import suspend_owned

        suspend_owned(self.journal, state, ownership_lost=ownership_lost)

    def step(self, state):
        if self.stop.is_set():
            return "wait"
        run = state["run"]["run"]
        if run.get("configuration_snapshot", {}).get("execution_kind") != "native":
            raise ReconciliationRequired("Only native task execution is supported.")
        try:
            engine = self.native_engine()
        except (ImportError, OSError, ValueError):
            raise ReconciliationRequired(
                "Native task dispatch requires configured isolated executor and validation profiles."
            ) from None
        return engine.step(state)
