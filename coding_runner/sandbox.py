import os
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .approvals import ActionSpec, ApprovalBroker, ApprovalRequired
from .workspaces import SourceStore, Workspace, WorkspaceError


class SandboxUnavailable(Exception):
    pass


def require_mediated_backend(*, transport: str, native_tools: bool) -> None:
    if transport != "structured_proposals" or native_tools:
        raise SandboxUnavailable(
            "Only structured action proposals through the approval broker are supported."
        )


def sandbox_command(
    checkout: Path, argv: tuple[str, ...], *, read_only: bool = False
) -> list[str]:
    command = [
        "/usr/bin/bwrap",
        "--unshare-all",
        "--unshare-user",
        "--die-with-parent",
        "--new-session",
        "--cap-drop",
        "ALL",
        "--disable-userns",
        "--clearenv",
        "--ro-bind",
        "/usr",
        "/usr",
        "--ro-bind",
        "/lib",
        "/lib",
        "--ro-bind",
        "/lib64",
        "/lib64",
        "--symlink",
        "usr/bin",
        "/bin",
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--tmpfs",
        "/tmp",
        "--dir",
        "/home",
        "--dir",
        "/home/agent",
        "--ro-bind" if read_only else "--bind",
        str(checkout),
        "/workspace",
        "--tmpfs",
        "/workspace/.git",
        "--chdir",
        "/workspace",
        "--setenv",
        "PATH",
        "/usr/bin:/bin",
        "--setenv",
        "HOME",
        "/home/agent",
        "--setenv",
        "LANG",
        "C.UTF-8",
        "--setenv",
        "TMPDIR",
        "/tmp",
        "--",
        *argv,
    ]
    return command


def probe_sandbox(checkout: Path) -> None:
    try:
        result = subprocess.run(
            sandbox_command(checkout, ("/usr/bin/true",)),
            env={},
            capture_output=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise SandboxUnavailable(
            "Bubblewrap namespaces are unavailable; execution remains disabled."
        ) from None
    if result.returncode:
        raise SandboxUnavailable(
            "Bubblewrap namespaces are unavailable; execution remains disabled."
        )


@dataclass(frozen=True)
class ExecutionResult:
    execution_id: str
    exit_code: int
    output: bytes
    interrupted: bool


class ApprovedExecutor:
    def __init__(
        self,
        broker: ApprovalBroker,
        sources: SourceStore,
        gate: Callable[[], bool],
        periodic_gate: Callable[[], bool] | None = None,
        *,
        read_only: bool = False,
    ):
        self.broker = broker
        self.sources = sources
        self.gate = gate
        self.periodic_gate = periodic_gate or gate
        self.read_only = read_only

    def execute(
        self,
        workspace: Workspace,
        action: ActionSpec,
        approval_id: str,
        *,
        timeout: float = 60,
    ) -> ExecutionResult:
        if not 0 < timeout <= 86400:
            raise ValueError("Command timeout is out of range.")
        if workspace.workspace_id != self.broker.run_id:
            raise ApprovalRequired("Workspace does not match the claimed run.")
        with workspace.locked():
            actual_digest = workspace.verify()
            source_digest = self.sources.current_digest(
                action.source_kind, action.source_reference, workspace
            )
            matches = (
                action.workspace_id == workspace.workspace_id
                and action.base_revision == workspace.base_revision
                and action.content_digest == actual_digest
                and action.source_digest == source_digest
            )
            if not matches:
                raise ApprovalRequired(
                    "Action, source, workspace content, or base changed; request approval again."
                )
            probe_sandbox(workspace.checkout)
            if self.gate() is not True:
                raise ApprovalRequired("Execution gate is closed.")
            execution_id = self.broker.consume(approval_id, action)
            if workspace.verify() != actual_digest:
                raise ApprovalRequired(
                    "Workspace changed while obtaining approval; request approval again."
                )
            if (
                self.sources.current_digest(
                    action.source_kind, action.source_reference, workspace
                )
                != source_digest
            ):
                raise ApprovalRequired(
                    "Source changed while obtaining approval; request approval again."
                )
            if self.gate() is not True:
                raise ApprovalRequired(
                    "Execution gate closed while obtaining approval."
                )
            return self._execute(workspace, action, execution_id, timeout)

    def _execute(
        self,
        workspace: Workspace,
        action: ActionSpec,
        execution_id: str,
        timeout: float,
    ) -> ExecutionResult:
        interrupted = False
        started = time.monotonic()
        with tempfile.TemporaryFile() as output:
            process = subprocess.Popen(
                sandbox_command(
                    workspace.checkout, action.argv, read_only=self.read_only
                ),
                env={},
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.STDOUT,
                close_fds=True,
                start_new_session=True,
            )
            try:
                while process.poll() is None:
                    expired = time.monotonic() - started >= timeout
                    oversized = output.tell() > 1048576
                    if expired or oversized or self.periodic_gate() is not True:
                        interrupted = True
                        break
                    time.sleep(min(0.1, timeout))
            finally:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            output.seek(0)
            captured = output.read(1048576)
        try:
            workspace.verify()
        except WorkspaceError:
            raise SandboxUnavailable(
                "Command left unsafe workspace content; operator recovery is required."
            ) from None
        return ExecutionResult(execution_id, process.returncode, captured, interrupted)
