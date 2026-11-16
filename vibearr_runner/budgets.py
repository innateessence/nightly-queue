import math
import time
from typing import Any, Callable, TypeVar

from .journal import Journal

T = TypeVar("T")


class BudgetExceeded(Exception):
    pass


class ActiveBudget:
    def __init__(
        self,
        journal: Journal,
        state: dict[str, Any],
        run_id: str,
        total_seconds: float,
        stage_seconds: dict[str, float],
        *,
        clock: Callable[[], float] = time.monotonic,
    ):
        values = [total_seconds, *stage_seconds.values()]
        if any(not math.isfinite(value) or value <= 0 for value in values):
            raise ValueError("Active budgets must be positive.")
        self.journal, self.state, self.clock = journal, state, clock
        self.limits: dict[str, Any] = {
            "total": total_seconds,
            "stages": dict(stage_seconds),
        }
        self.data = state.setdefault("budgets", {}).setdefault(
            run_id, {"limits": self.limits, "total": 0.0, "stages": {}, "active": None}
        )
        if self.data["limits"] != self.limits:
            raise BudgetExceeded("Frozen execution budgets changed.")
        self.started: float | None = None
        self.stage: str | None = None

    def remaining(self, stage: str | None) -> float:
        if self.data["active"] is not None and self.started is None:
            raise BudgetExceeded(
                "Interrupted active budget requires recovery before more execution."
            )
        elapsed = 0.0 if self.started is None else max(0.0, self.clock() - self.started)
        stage_elapsed = elapsed if stage == self.stage else 0.0
        total_left = self.limits["total"] - self.data["total"] - elapsed
        if stage is None:
            return max(0.0, total_left)
        stage_left = (
            self.limits["stages"][stage]
            - self.data["stages"].get(stage, 0.0)
            - stage_elapsed
        )
        return max(0.0, min(total_left, stage_left))

    def checkpoint(self) -> bool:
        if self.started is None:
            return (
                self.data["active"] is None
                and self.data["total"] < self.limits["total"]
            )
        return self.remaining(self.stage) > 0

    def run(self, stage: str | None, operation: Callable[[float], T]) -> T:
        remaining = self.remaining(stage)
        if remaining <= 0:
            raise BudgetExceeded("Active task or stage timeout exhausted.")
        if self.started is not None:
            raise BudgetExceeded("Active budget segments cannot overlap.")
        self.stage, self.started = stage, self.clock()
        self.data["active"] = {"stage": stage, "started": self.started}
        self.journal.save(self.state)
        try:
            result = operation(remaining)
            if not self.checkpoint():
                raise BudgetExceeded("Active task or stage timeout exhausted.")
            return result
        finally:
            elapsed = max(0.0, self.clock() - self.started)
            self.data["total"] += elapsed
            if stage is not None:
                self.data["stages"][stage] = (
                    self.data["stages"].get(stage, 0.0) + elapsed
                )
            self.data["active"] = None
            self.started, self.stage = None, None
            self.journal.save(self.state)
