"""Supervisor-facing backend contracts. Readiness never grants release permission."""

from dataclasses import dataclass
from datetime import datetime
from threading import Event
from typing import Any, Callable, Protocol

from .gates import UsageReading


class BackendError(Exception):
    pass


class BackendAuthenticationError(BackendError):
    pass


class BackendInterrupted(BackendError):
    pass


class BackendTimeout(BackendError):
    pass


class BackendOutputError(BackendError):
    pass


@dataclass(frozen=True)
class BackendCapabilities:
    transport: str
    native_tools: bool
    research: bool
    implementation: bool
    quota: bool
    authentication_kinds: tuple[str, ...]
    graceful_interrupt: bool


@dataclass(frozen=True)
class BackendReadiness:
    ready: bool
    authenticated: bool
    reasons: tuple[str, ...]
    capabilities: BackendCapabilities


@dataclass(frozen=True)
class BackendRequest:
    account: dict[str, Any]
    model: str
    instructions: str
    document: dict[str, Any]
    timeout_seconds: float = 60


@dataclass(frozen=True)
class BackendEvent:
    kind: str
    data: dict[str, Any]


Progress = Callable[[BackendEvent], None]
Checkpoint = Callable[[], bool]


class StageBudget(Protocol):
    def run(self, stage: str, operation: Callable[[float], Any]) -> Any: ...

    def checkpoint(self) -> bool: ...


class Backend(Protocol):
    def readiness(self, account: dict[str, Any]) -> BackendReadiness: ...

    def research(
        self,
        request: BackendRequest,
        *,
        progress: Progress,
        checkpoint: Checkpoint,
        stop: Event,
    ) -> BackendEvent: ...

    def implementation(
        self,
        request: BackendRequest,
        *,
        progress: Progress,
        checkpoint: Checkpoint,
        stop: Event,
    ) -> BackendEvent: ...

    def read_usage(self, account: dict[str, Any], now: datetime) -> UsageReading: ...

    def interrupt(self) -> None: ...
