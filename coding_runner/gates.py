"""Processing policy only; callers still need release, ownership, and sandbox checks."""

import hashlib
import math
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Callable, Protocol
from zoneinfo import ZoneInfo

from .journal import Journal, JournalError


AccountKey = tuple[str, int | None]


def utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("An aware clock is required.")
    return value.astimezone(UTC)


def stamp(value: datetime | None) -> str | None:
    return None if value is None else utc(value).isoformat()


@dataclass(frozen=True)
class UsageWindow:
    window_id: str
    remaining_percent: float
    observed_at: datetime
    reset_at: datetime | None = None
    unit: str = "percent"


@dataclass(frozen=True)
class UsageReading:
    supported: bool
    windows: tuple[UsageWindow, ...] = ()


class UsageAdapter(Protocol):
    def read_usage(self, account: dict[str, Any], now: datetime) -> UsageReading: ...


@dataclass(frozen=True)
class Interval:
    id: str
    start: datetime
    end: datetime

    def data(self) -> dict[str, Any]:
        return {"id": self.id, "start": stamp(self.start), "end": stamp(self.end)}


@dataclass(frozen=True)
class GateDecision:
    allowed: bool
    reasons: tuple[str, ...]
    checked_at: str
    checkpoint: str
    run_id: str | None
    configuration_revision: str
    interval: dict[str, Any] | None
    next_boundary: str | None
    next_check_at: str
    reserve_enabled: bool
    minimum_remaining_usage_percent: float
    readings: tuple[dict[str, Any], ...]
    tasks_started: int

    def data(self) -> dict[str, Any]:
        result = asdict(self)
        result["schema_version"] = 1
        result["reasons"] = list(self.reasons)
        result["readings"] = list(self.readings)
        return result


def boundary(day: date, text: str, zone: ZoneInfo, opening: bool) -> datetime:
    local = datetime.combine(day, time.fromisoformat(text))
    # Minute precision matches configuration; also handles a skipped calendar day.
    for _ in range(2881):
        candidates = []
        for fold in (0, 1):
            value = local.replace(tzinfo=zone, fold=fold).astimezone(UTC)
            if value.astimezone(zone).replace(tzinfo=None) == local:
                candidates.append(value)
        if candidates:
            return min(candidates) if opening else max(candidates)
        local += timedelta(minutes=1)
    raise ValueError("Timezone boundary cannot be resolved.")


def merged(
    intervals: list[tuple[datetime, datetime]],
) -> list[tuple[datetime, datetime]]:
    result: list[tuple[datetime, datetime]] = []
    for start, end in sorted(intervals):
        if end <= start:
            continue
        if result and start <= result[-1][1]:
            result[-1] = (result[-1][0], max(end, result[-1][1]))
        else:
            result.append((start, end))
    return result


def covers_week(windows: list[dict[str, Any]]) -> bool:
    minutes: set[int] = set()
    for window in windows:
        start = time.fromisoformat(window["start"])
        end = time.fromisoformat(window["end"])
        first = start.hour * 60 + start.minute
        last = end.hour * 60 + end.minute
        duration = (last - first) % 1440
        for day in window["days"]:
            minutes.update(
                (day * 1440 + first + offset) % 10080 for offset in range(duration)
            )
    return len(minutes) == 10080


def schedule(
    controls: dict[str, Any], now: datetime
) -> tuple[Interval | None, datetime | None]:
    zone = ZoneInfo(controls["timezone"])
    today = now.astimezone(zone).date()
    mode = controls["schedule_mode"]
    windows = controls["windows"]
    if mode not in {"continuous", "windows"}:
        raise ValueError("Unknown schedule mode.")
    if mode == "continuous" or covers_week(windows):
        start = boundary(today, "00:00", zone, True)
        end = boundary(today + timedelta(days=1), "00:00", zone, True)
        identity = f"day:{zone.key}:{today.isoformat()}"
        return Interval(identity, start, end), end
    intervals = []
    # A weekly component with a gap is shorter than a week, including overnight edges.
    for offset in range(-8, 9):
        day = today + timedelta(days=offset)
        for window in windows:
            if day.weekday() not in window["days"]:
                continue
            end_day = day + timedelta(days=window["end"] <= window["start"])
            start = boundary(day, window["start"], zone, True)
            end = boundary(end_day, window["end"], zone, False)
            intervals.append((start, end))
    for start, end in merged(intervals):
        if start <= now < end:
            identity = f"{zone.key}:{stamp(start)}:{stamp(end)}"
            digest = hashlib.sha256(identity.encode()).hexdigest()
            return Interval(f"window:{digest}", start, end), end
        if start > now:
            return None, start
    return None, None


class GateEvaluator:
    def __init__(
        self,
        journal: Journal,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        adapters: dict[AccountKey, UsageAdapter] | None = None,
        max_usage_age_seconds: int = 300,
    ):
        if max_usage_age_seconds <= 0:
            raise ValueError("Usage freshness must be positive.")
        self.journal = journal
        self.clock = clock
        self.adapters = {} if adapters is None else adapters
        self.max_usage_age_seconds = max_usage_age_seconds

    def evaluate(
        self,
        configuration: dict[str, Any],
        state: dict[str, Any],
        checkpoint: str = "claim",
        run_id: str | None = None,
        active_count: int = 0,
        capacity: int = 1,
    ) -> GateDecision:
        if checkpoint not in {"claim", "stage", "model", "periodic"}:
            raise ValueError("Unknown gate checkpoint.")
        if checkpoint != "claim" and not run_id:
            raise ValueError("Owned checkpoints require a run ID.")
        now = utc(self.clock())
        controls = configuration["global"]
        gate = state.setdefault("gates", {"counts": {}, "attempts": {}, "waits": {}})
        interval, next_boundary = schedule(controls, now)
        reasons = []
        if not configuration.get("execution_ready", False):
            reasons.append("release_disabled")
        if not controls["enabled"]:
            reasons.append("disabled")
        if controls["paused"]:
            reasons.append("paused")
        if interval is None:
            reasons.append("outside_window")
        owned = state.get("remote_active_run_id") or gate.get("owned_run_id")
        if checkpoint == "claim" and (
            gate.get("pending_claim") or state.get("operation")
        ):
            reasons.append("reconciliation_required")
        if checkpoint == "claim" and owned:
            reasons.append("resume_owned_run_first")
        if checkpoint != "claim" and owned != run_id:
            reasons.append("ownership_unknown")
        elif checkpoint != "claim" and run_id not in gate["attempts"]:
            reasons.append("claim_accounting_unknown")
        if (
            capacity < 1
            or active_count < 0
            or checkpoint == "claim"
            and active_count >= capacity
        ):
            reasons.append("capacity")
        started = 0 if interval is None else gate["counts"].get(interval.id, 0)
        if checkpoint == "claim" and started >= controls["tasks_per_window"]:
            reasons.append("window_limit")
        usage_configuration = {
            **configuration,
            "accounts": list(configuration["accounts"]),
        }
        if run_id:
            original_accounts = gate.get("attempt_accounts", {}).get(run_id, [])
            usage_configuration["accounts"].extend(original_accounts)
        readings, usage_reasons = self._usage(usage_configuration, now)
        reasons.extend(usage_reasons)
        poll = timedelta(seconds=controls["polling_interval_seconds"])
        next_check = now + poll
        if next_boundary is not None:
            next_check = min(next_check, next_boundary)
        for reading in readings:
            for window in reading["windows"]:
                expiry = datetime.fromisoformat(window["observed_at"]) + timedelta(
                    seconds=self.max_usage_age_seconds
                )
                if now < expiry:
                    next_check = min(next_check, expiry)
                if window["reset_at"] and now < datetime.fromisoformat(
                    window["reset_at"]
                ):
                    next_check = min(
                        next_check, datetime.fromisoformat(window["reset_at"])
                    )
        if utc(self.clock()) >= next_check:
            reasons.append("checkpoint_expired")
        decision = GateDecision(
            not reasons,
            tuple(dict.fromkeys(reasons)),
            stamp(now) or "",
            checkpoint,
            run_id,
            configuration["revision"],
            None if interval is None else interval.data(),
            stamp(next_boundary),
            stamp(next_check) or "",
            controls["reserve_enabled"],
            controls["minimum_remaining_usage_percent"],
            tuple(readings),
            started,
        )
        gate["checkpoint"] = decision.data()
        if run_id:
            self._wait(gate, run_id, decision, now)
        self.journal.save(state)
        return decision

    def _usage(self, configuration: dict[str, Any], now: datetime):
        controls = configuration["global"]
        enforced = controls["reserve_enabled"]
        reserve = controls["minimum_remaining_usage_percent"]
        reports = []
        reasons = []
        accounts = configuration["accounts"]
        if enforced and not accounts:
            reasons.append("usage_missing_accounts")
        seen = set()
        for account in accounts:
            key = (account["backend"], account["credential_id"])
            if key in seen:
                continue
            seen.add(key)
            report = {
                "backend": key[0],
                "credential_id": key[1],
                "status": "missing",
                "windows": [],
            }
            adapter = self.adapters.get(key)
            if adapter is None:
                reports.append(report)
                if enforced:
                    reasons.append("usage_missing")
                continue
            try:
                reading = adapter.read_usage(account, now)
                self._reading(report, reading, now, reserve)
            except Exception:
                # Provider errors never become an unlimited allowance or leak provider text.
                report["status"] = "unavailable"
            reports.append(report)
            if enforced and report["status"] != "ready":
                reasons.append(f"usage_{report['status']}")
        return reports, reasons

    def _reading(self, report, reading: UsageReading, now: datetime, reserve: float):
        if not reading.supported:
            report["status"] = "unsupported"
            return
        if not reading.windows:
            report["status"] = "missing"
            return
        statuses = []
        seen = set()
        for window in reading.windows:
            observed = utc(window.observed_at)
            reset = None if window.reset_at is None else utc(window.reset_at)
            remaining = window.remaining_percent
            status = "ready"
            if (
                not window.window_id
                or window.window_id in seen
                or window.unit != "percent"
            ):
                status = "invalid"
            elif (
                isinstance(remaining, bool)
                or not math.isfinite(remaining)
                or not 0 <= remaining <= 100
            ):
                status = "invalid"
            elif (
                observed > now
                or (now - observed).total_seconds() >= self.max_usage_age_seconds
            ):
                status = "stale"
            elif reset is not None and reset <= now:
                status = "stale"
            elif remaining <= reserve:
                status = "reserve"
            seen.add(window.window_id)
            statuses.append(status)
            safe_remaining = remaining if status != "invalid" else None
            report["windows"].append(
                {
                    "window_id": window.window_id,
                    "remaining_percent": safe_remaining,
                    "observed_at": stamp(observed),
                    "reset_at": stamp(reset),
                    "unit": window.unit,
                    "status": status,
                }
            )
        report["status"] = next(
            (status for status in statuses if status != "ready"), "ready"
        )

    def prepare_claim(
        self, state: dict[str, Any], run_id: str, decision: GateDecision
    ) -> None:
        if (
            not decision.allowed
            or decision.checkpoint != "claim"
            or decision.interval is None
        ):
            raise JournalError("An allowed claim checkpoint is required.")
        gate = state["gates"]
        if utc(self.clock()) >= datetime.fromisoformat(decision.next_check_at):
            raise JournalError("The claim checkpoint has expired.")
        if gate.get("pending_claim"):
            raise JournalError("Reconcile the pending claim first.")
        if gate.get("checkpoint") != decision.data():
            raise JournalError("The claim checkpoint has been superseded.")
        gate["pending_claim"] = {
            "run_id": run_id,
            "interval_id": decision.interval["id"],
            "accounts": [
                {"backend": row["backend"], "credential_id": row["credential_id"]}
                for row in decision.readings
            ],
        }
        self.journal.save(state)

    def record_claim(self, state: dict[str, Any], run_id: str) -> None:
        gate = state["gates"]
        if run_id in gate["attempts"]:
            return
        pending = gate.get("pending_claim")
        if not pending or pending["run_id"] != run_id:
            raise JournalError("Reconcile the original claim interval before counting.")
        identity = pending["interval_id"]
        gate["attempts"][run_id] = identity
        gate["counts"][identity] = gate["counts"].get(identity, 0) + 1
        gate.setdefault("attempt_accounts", {})[run_id] = pending["accounts"]
        gate["owned_run_id"] = run_id
        gate["pending_claim"] = None
        self.journal.save(state)

    def abandon_claim(self, state: dict[str, Any], run_id: str) -> None:
        gate = state["gates"]
        pending = gate.get("pending_claim")
        if not pending or pending["run_id"] != run_id:
            raise JournalError("Claim reconciliation does not match.")
        gate["pending_claim"] = None
        self.journal.save(state)

    def complete_run(self, state: dict[str, Any], run_id: str) -> None:
        gate = state["gates"]
        if gate.get("owned_run_id") != run_id:
            raise JournalError("Completion does not match the owned run.")
        self._wait(gate, run_id, None, utc(self.clock()))
        gate["owned_run_id"] = None
        if state.get("remote_active_run_id") == run_id:
            state["remote_active_run_id"] = None
        self.journal.save(state)

    def _wait(self, gate, run_id, decision, now):
        wait = gate["waits"].setdefault(
            run_id, {"paused_seconds": 0, "started_at": None}
        )
        if decision is not None and not decision.allowed:
            if wait["started_at"] is None:
                wait["started_at"] = stamp(now)
            wait["checkpoint"] = decision.checkpoint
            wait["reasons"] = list(decision.reasons)
            return
        if wait["started_at"] is not None:
            start = datetime.fromisoformat(wait["started_at"])
            wait["paused_seconds"] += max(0, (now - start).total_seconds())
            wait["started_at"] = None
        wait["reasons"] = []

    def paused_seconds(self, state: dict[str, Any], run_id: str) -> float:
        wait = state.get("gates", {}).get("waits", {}).get(run_id, {})
        elapsed = wait.get("paused_seconds", 0)
        if wait.get("started_at"):
            start = datetime.fromisoformat(wait["started_at"])
            elapsed += max(0, (utc(self.clock()) - start).total_seconds())
        return elapsed
