import fcntl
import os
import shutil
from pathlib import Path
from typing import Any, Callable
from uuid import UUID

from .client import Client, InvalidResponse
from .journal import Journal, JournalError
from datetime import UTC, datetime

MINIMUM_FREE_BYTES = 1024 * 1024 * 1024


class Maintenance:
    def __init__(
        self,
        client: Client,
        journal: Journal,
        workspaces: Path,
        *,
        disk_usage: Callable = shutil.disk_usage,
    ):
        self.client = client
        self.journal = journal
        self.workspaces = workspaces
        self.disk_usage = disk_usage

    def acknowledge(
        self, state, run_id: str, claim_token: str, finish_digest: str
    ) -> None:
        if (
            state.get("operation")
            or (state.get("request") or {}).get("status") == "pending"
        ):
            raise JournalError(
                "Unresolved metadata operations protect completion evidence."
            )
        if any(
            record.get("status") != "stopped"
            for record in state.get("processes", {}).values()
        ):
            raise JournalError(
                "Confirm every child process stopped before completion acknowledgement."
            )
        run_id = str(UUID(run_id))
        self.client.request(
            "POST",
            f"/api/v1/runner/runs/{run_id}/retention/ack/",
            {
                "claim_token": claim_token,
                "finish_digest": finish_digest,
            },
        )
        self.compact(state, run_id, finish_digest)

    def compact(self, state, run_id, finish_digest):
        for key in ("budgets",):
            state.get(key, {}).pop(run_id, None)
        state["processes"] = {
            key: record
            for key, record in state.get("processes", {}).items()
            if record.get("status") != "stopped"
        }
        gate = state.get("gates", {})
        for key in ("attempts", "attempt_accounts", "waits"):
            gate.get(key, {}).pop(run_id, None)
        referenced = set(gate.get("attempts", {}).values())
        pending = gate.get("pending_claim") or {}
        if pending.get("interval_id"):
            referenced.add(pending["interval_id"])
        current = (gate.get("checkpoint", {}).get("interval") or {}).get("id")
        if current:
            referenced.add(current)
        for interval_id, end in list(gate.get("interval_ends", {}).items()):
            if interval_id not in referenced and datetime.fromisoformat(
                end
            ) <= datetime.now(UTC):
                gate.get("counts", {}).pop(interval_id, None)
                gate["interval_ends"].pop(interval_id, None)
        receipts = state.setdefault("completion_receipts", [])
        receipt = {"run_id": run_id, "finish_digest": finish_digest}
        if receipt not in receipts:
            receipts.append(receipt)
        state["completion_receipts"] = receipts[-100:]
        self.journal.save(state)

    def __call__(self, state: dict[str, Any], configuration: dict[str, Any]) -> bool:
        report = self.client.request("POST", "/api/v1/runner/retention/", {})
        candidates = report.get("workspaces")
        if (
            report.get("schema_version") != 1
            or not isinstance(candidates, list)
            or len(candidates) > 100
        ):
            raise InvalidResponse(
                "Retention plan is invalid; local data was preserved."
            )
        counters = state.setdefault(
            "maintenance", {"removed": 0, "missing": 0, "busy": 0}
        )
        protected = {
            state.get("remote_active_run_id"),
            (state.get("run") or {}).get("run", {}).get("id"),
        }
        if state.get("operation"):
            protected.add(state["operation"].get("run_id"))
        for resource, candidates in (
            ("workspace", candidates),
            ("logs", report.get("logs", [])),
        ):
            if not isinstance(candidates, list) or len(candidates) > 100:
                raise InvalidResponse("Retention candidates are invalid.")
            for candidate in candidates:
                run_id = str(UUID(candidate))
                if candidate != run_id:
                    raise InvalidResponse("Retention workspace identity is invalid.")
                if run_id in protected:
                    continue
                from .native_resources import NativeResources

                resources = NativeResources(self.journal.directory)
                if resources.path(run_id).exists():
                    from vibearr_executor.client import Client as ExecutorClient

                    resources.purge(run_id, ExecutorClient.from_environment(), resource)
                root = (
                    self.workspaces
                    if resource == "workspace"
                    else self.journal.directory / "logs"
                )
                status = self.remove(run_id, root)
                counters[status] = counters.get(status, 0) + 1
                self.journal.save(state)
                if status == "busy":
                    continue
                self.client.request(
                    "POST",
                    f"/api/v1/runner/runs/{run_id}/retention/workspace/",
                    {"status": status, "resource": resource},
                )
        free = self.disk_usage(self.journal.directory).free
        counters["free_bytes"] = free
        counters["minimum_free_bytes"] = MINIMUM_FREE_BYTES
        counters["blocking_reason"] = (
            ""
            if free >= MINIMUM_FREE_BYTES
            else "Disk pressure: less than 1 GiB free; preserve recoverable work and expand or clean eligible storage."
        )
        self.journal.save(state)
        self.client.request("POST", "/api/v1/runner/health/", counters)
        return not counters["blocking_reason"]

    def blocked(self, state, reason):
        counters = dict(
            state.get("maintenance", {"removed": 0, "missing": 0, "busy": 0})
        )
        counters.update(
            free_bytes=self.disk_usage(self.journal.directory).free,
            minimum_free_bytes=MINIMUM_FREE_BYTES,
            blocking_reason=reason[:500],
        )
        self.client.request("POST", "/api/v1/runner/health/", counters)

    def remove(self, run_id: str, root: Path) -> str:
        directory = root / run_id
        if root.is_symlink() or directory.is_symlink():
            raise InvalidResponse("Retention refuses symbolic-link workspace roots.")
        if not directory.exists():
            return "missing"
        descriptor = os.open(
            directory / "workspace.lock",
            os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
        )
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return "busy"
            shutil.rmtree(directory)
        finally:
            os.close(descriptor)
        return "removed"
