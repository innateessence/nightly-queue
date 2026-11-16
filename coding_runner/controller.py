import logging
import threading
from typing import Any

from .client import (
    APIError,
    AuthenticationError,
    Client,
    InvalidResponse,
    NotFoundError,
)
from .journal import Journal, JournalError

logger = logging.getLogger(__name__)


class ControllerStopped(Exception):
    pass


class Controller:
    def __init__(
        self, client: Client, journal: Journal, stop: threading.Event | None = None
    ):
        self.client = client
        self.journal = journal
        self.stop = stop if stop is not None else threading.Event()

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

    def reconcile(self, state: dict[str, Any]) -> None:
        operation = state.get("operation")
        if not operation or operation.get("status") != "pending":
            return
        self.check_shutdown()
        try:
            result = self.client.reconcile(operation["run_id"])
        except NotFoundError:
            result = {"reconciliation": "not_found"}
        operation["reconciliation"] = result
        self.journal.save(state)
        logger.warning("Pending operation requires recovery; no mutation was repeated.")

    def run(self, check: bool = False) -> None:
        state = self.journal.load()
        delay = 1
        while not self.stop.is_set():
            try:
                configuration = self.probe(state)
                self.reconcile(state)
                if check:
                    logger.info(
                        "Authenticated startup check passed; execution remains disabled."
                    )
                    return
                poll = configuration["global"].get("polling_interval_seconds", 30)
                if type(poll) is not int or not 1 <= poll <= 86400:
                    raise InvalidResponse("Polling interval is invalid.")
                delay = 1
                logger.info(
                    "Connected; execution remains disabled pending controlled release."
                )
                self.stop.wait(poll)
            except AuthenticationError:
                raise
            except ControllerStopped:
                return
            except APIError as exc:
                if check:
                    raise
                logger.warning(
                    "Queue probe failed (%s); retrying in %s seconds.",
                    type(exc).__name__,
                    delay,
                )
                self.stop.wait(delay)
                delay = min(delay * 2, 60)

    def check_shutdown(self) -> None:
        if self.stop.is_set():
            raise ControllerStopped


def journaled_request(
    client: Client, journal: Journal, state, run_id: str, kind: str, path: str, payload
):
    """An unresolved operation blocks all further writes, including after a crash."""
    journal.prepare(state, run_id, kind, payload)
    result = client.request("POST", path, payload)
    operation = state["operation"]
    operation["status"] = "received"
    operation["response"] = result
    journal.save(state)
    return result
