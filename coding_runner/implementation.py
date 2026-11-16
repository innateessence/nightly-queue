import hashlib
import os
import stat
import tempfile
import threading
from contextlib import nullcontext
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable
from uuid import UUID, uuid4

from .approvals import ActionSpec, digest
from .backends import (
    Backend,
    BackendError,
    BackendEvent,
    BackendOutputError,
    BackendRequest,
)
from .budgets import ActiveBudget, BudgetExceeded
from .context import canonical_bytes
from .journal import Journal
from .plans import validate_context, validate_plan
from .research import checkout_inventory, read_checkout
from .workspaces import SourceStore, Workspace, WorkspaceError, git


@dataclass(frozen=True)
class ImplementationResult:
    status: str
    detail: dict[str, Any]


def tree_entries(workspace: Workspace, revision: str) -> list[list[str]]:
    entries = git(["ls-tree", "-r", "-z", "--full-tree", revision], workspace.checkout)
    result = []
    for entry in entries.split("\0"):
        if not entry:
            continue
        metadata, path = entry.split("\t", 1)
        mode, kind, object_id = metadata.split(" ")
        if kind != "blob":
            raise WorkspaceError("Submodule trees require explicit operator recovery.")
        result.append([path, mode, object_id])
    return sorted(result)


def working_tree_entries(workspace: Workspace) -> list[list[str]]:
    names = git(
        ["ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        workspace.checkout,
    )
    algorithm = git(["rev-parse", "--show-object-format"], workspace.checkout)
    result = []
    for name in sorted(set(names.split("\0")) - {""}):
        path = workspace.checkout / name
        if any(
            (workspace.checkout / parent).is_symlink() for parent in Path(name).parents
        ):
            raise WorkspaceError("Git paths cannot traverse symbolic links.")
        if not path.exists() and not path.is_symlink():
            continue
        details = path.lstat()
        if stat.S_ISLNK(details.st_mode):
            content = os.fsencode(os.readlink(path))
            blob = hashlib.new(algorithm, f"blob {len(content)}\0".encode())
            blob.update(content)
            mode = "120000"
        elif stat.S_ISREG(details.st_mode) and details.st_nlink == 1:
            blob = hashlib.new(algorithm, f"blob {details.st_size}\0".encode())
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(descriptor, "rb") as stream:
                while chunk := stream.read(65536):
                    blob.update(chunk)
            mode = "100755" if details.st_mode & 0o111 else "100644"
        else:
            raise WorkspaceError("Delivery requires regular files or symbolic links.")
        result.append([name, mode, blob.hexdigest()])
    return result


def workspace_evidence(workspace: Workspace) -> dict[str, str]:
    content = workspace.verify()
    if git(["ls-files", "--unmerged"], workspace.checkout):
        raise WorkspaceError("Unresolved conflicts require operator attention.")
    branch = git(["symbolic-ref", "--short", "HEAD"], workspace.checkout)
    if branch != workspace.branch_name:
        raise WorkspaceError("Task branch changed; prior work was preserved.")
    head = git(["rev-parse", "--verify", "HEAD^{commit}"], workspace.checkout)
    git(
        ["merge-base", "--is-ancestor", workspace.base_revision, head],
        workspace.checkout,
    )
    diff = git(
        [
            "diff",
            "--no-ext-diff",
            "--no-textconv",
            "--binary",
            workspace.base_revision,
            "--",
        ],
        workspace.checkout,
    )
    untracked = git(["ls-files", "--others", "--exclude-standard"], workspace.checkout)
    return {
        "workspace_id": workspace.workspace_id,
        "base_revision": workspace.base_revision,
        "branch": branch,
        "head_revision": head,
        "content_digest": content,
        "tree_digest": digest(working_tree_entries(workspace)),
        "diff_digest": digest(
            {"base_revision": workspace.base_revision, "content_digest": content}
        ),
        "changed": "yes" if diff or untracked else "no",
    }


def verify_delivery_ready(
    workspace: Workspace,
    evidence: dict[str, Any],
    *,
    committed_head: str | None = None,
    already_locked: bool = False,
) -> dict[str, Any]:
    with nullcontext() if already_locked else workspace.locked():
        current = workspace_evidence(workspace)
        validated = evidence["workspace"]
        expected = (
            {**validated, "head_revision": committed_head}
            if committed_head
            else validated
        )
        if current != expected or current["changed"] != "yes":
            raise BackendError(
                "Validated files or revision changed; project checks are stale."
            )
        checks = evidence.get("validation")
        if not checks or any(
            check["exit_code"] != 0
            or check["interrupted"]
            or check["phase"] != "validation"
            or check["before"] != validated
            or check["after"] != validated
            for check in checks
        ):
            raise BackendError(
                "Delivery requires passing project checks for the exact validated diff."
            )
        if committed_head and git(
            ["status", "--porcelain", "--untracked-files=all"], workspace.checkout
        ):
            raise BackendError(
                "Supervised commit did not include the complete validated task changes."
            )
        if (
            committed_head
            and digest(tree_entries(workspace, committed_head))
            != validated["tree_digest"]
        ):
            raise BackendError(
                "Committed tree does not match the validated file bytes and modes."
            )
    return {
        "validated_head": validated["head_revision"],
        "committed_head": current["head_revision"],
        "workspace": current,
    }


def apply_patch(workspace: Workspace, proposal: dict[str, Any]) -> None:
    from .codex_backend import validate_proposal

    validate_proposal("patch", proposal)
    if (
        proposal["base_revision"] != workspace.base_revision
        or proposal["content_digest"] != workspace.verify()
    ):
        raise BackendOutputError("Patch does not bind the current checkout.")
    prepared = []
    directories: set[Path] = set()
    proposed_paths = {entry["path"] for entry in proposal["files"]}
    for entry in proposal["files"]:
        relative = Path(entry["path"])
        path = workspace.checkout / relative
        for parent in reversed(relative.parents):
            directory = workspace.checkout / parent
            if parent.as_posix() in proposed_paths:
                raise BackendOutputError(
                    "Patch cannot replace a proposed file's parent directory."
                )
            if directory.is_symlink() or (
                directory.exists() and not directory.is_dir()
            ):
                raise BackendOutputError(
                    "Patch parent must stay within visible checkout directories."
                )
            if not directory.exists():
                directories.add(directory)
        if path.is_symlink():
            raise BackendOutputError("Patch cannot replace symbolic links.")
        if path.exists():
            raw = read_checkout(workspace, entry["path"])
            if hashlib.sha256(raw).hexdigest() != entry["expected_digest"]:
                raise BackendOutputError(
                    "Patch conflicts with existing work; nothing was replaced."
                )
            mode = stat.S_IMODE(path.stat().st_mode)
        else:
            if entry["expected_digest"] is not None or entry["content"] is None:
                raise BackendOutputError("Patch creation must name an absent file.")
            mode = 0o644
        prepared.append((path, entry["content"], mode))
    for directory in sorted(directories, key=lambda path: len(path.parts)):
        directory.mkdir(mode=0o755)
    for path, content, mode in prepared:
        if content is None:
            path.unlink()
            continue
        descriptor, temporary = tempfile.mkstemp(
            prefix=".runner-patch-", dir=path.parent
        )
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content.encode("utf-8"))
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, mode)
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    workspace.verify()


class ImplementationStage:
    def __init__(
        self,
        backend: Backend,
        journal: Journal,
        store: Any,
        executor: Any,
        sources: SourceStore,
        budget: ActiveBudget,
        snapshot: dict[str, Any],
        *,
        checkpoint: Callable[[], bool],
        progress: Callable[[BackendEvent], None],
        stop: threading.Event | None = None,
    ):
        self.backend, self.journal, self.store = backend, journal, store
        self.executor, self.sources, self.budget = executor, sources, budget
        self.snapshot = snapshot
        self.checkpoint, self.progress = checkpoint, progress
        self.stop = stop or threading.Event()
        previous_gate = executor.gate
        previous_periodic = executor.periodic_gate
        executor.gate = lambda: previous_gate() is True and self._periodic()
        executor.periodic_gate = (
            lambda: previous_periodic() is True and self._periodic()
        )

    def _allowed(self) -> bool:
        return not self.stop.is_set() and self.checkpoint() is True

    def revalidate(self, state: dict[str, Any], run_id: str) -> None:
        session = state["implementation"][run_id]
        if session["pending"] is not None or session["model_pending"]:
            raise BackendError(
                "Resolve pending execution before requesting validation again."
            )
        if session["phase"] == "setup":
            raise BackendError(
                "Complete required setup before requesting validation again."
            )
        phase = "setup" if session.get("setup_retry") else "review"
        session.update({"phase": phase, "index": 0, "checks": [], "review": None})
        self.journal.save(state)

    def _periodic(self) -> bool:
        return self._allowed() and self.budget.checkpoint()

    def step(
        self,
        state: dict[str, Any],
        run_id: str,
        item: dict[str, Any],
        workspace: Workspace,
        request: BackendRequest,
        *,
        issue_client: Any = None,
    ) -> ImplementationResult:
        saved = state.get("implementation", {}).get(run_id, {})
        if saved.get("phase") == "failed":
            return ImplementationResult("failed", {"reason": saved["failure"]})
        if not self._allowed():
            self.backend.interrupt()
            return ImplementationResult(
                "wait", {"reason": "Execution control checkpoint is closed."}
            )
        try:
            return self._step(state, run_id, item, workspace, request, issue_client)
        except BudgetExceeded as exc:
            self.backend.interrupt()
            session = state.setdefault("implementation", {}).setdefault(run_id, {})
            session.update({"phase": "failed", "failure": str(exc)})
            self.journal.save(state)
            return ImplementationResult("failed", {"reason": str(exc)})

    def _step(self, state, run_id, item, workspace, request, issue_client):
        if run_id != workspace.workspace_id:
            raise BackendError("Run and task workspace do not match.")
        session = state.get("implementation", {}).get(run_id, {})
        pending = session.get("pending")
        if pending and pending["kind"] == "action" and not pending.get("started"):
            action = ActionSpec.from_data(pending["data"]["action"])
            with workspace.locked():
                unchanged = action.content_digest == workspace.verify()
                unchanged = (
                    unchanged
                    and action.source_digest
                    == self.sources.current_digest(
                        action.source_kind, action.source_reference, workspace
                    )
                )
            if unchanged:
                approval = self.executor.broker.request(action, pending["operation_id"])
                if approval["decision"] != "approved":
                    return ImplementationResult(
                        "approval",
                        {
                            "approval_id": approval["id"],
                            "decision": approval["decision"],
                        },
                    )
        handoff = self.budget.run(
            "research",
            lambda remaining: self.store.handoff(
                item,
                workspace.base_revision,
                issue_client=issue_client,
                resolved_base_branch=workspace.resolved_base_branch,
            ),
        )
        plan, context = handoff["plan"], handoff["context"]
        document = plan["document"]
        validate_context(context["document"], item)
        if context["digest"] != digest(context["document"]):
            raise BackendOutputError("Context digest changed.")
        context_id = str(UUID(document["context_id"]))
        origin = self.store.client.request(
            "GET", f"/api/v1/runner/runs/{run_id}/contexts/{context_id}/"
        )["context"]
        validate_context(origin["document"], item)
        validate_plan(
            document, origin["document"], origin["id"], workspace.base_revision
        )
        old_discussion = {
            key: value
            for key, value in origin["document"].items()
            if key != "fetched_at"
        }
        new_discussion = {
            key: value
            for key, value in context["document"].items()
            if key != "fetched_at"
        }
        if (
            old_discussion != new_discussion
            or context["inspected_base_commit"] != workspace.base_revision
        ):
            raise BackendError(
                "Discussion or inspected base changed; revise the saved plan."
            )
        if (
            document["blockers"]
            or document["resolved_base_branch"] != workspace.resolved_base_branch
        ):
            raise BackendError(
                "Saved plan is blocked or names a different resolved base."
            )
        if plan.get("digest") != digest(document):
            raise BackendOutputError("Saved plan digest changed.")
        binding = digest(
            {
                "snapshot": self.snapshot,
                "account": request.account,
                "model": request.model,
                "instructions": request.instructions,
            }
        )
        session = state.setdefault("implementation", {}).setdefault(
            run_id,
            {
                "binding": binding,
                "plan_id": plan["id"],
                "plan_digest": plan["digest"],
                "phase": "setup",
                "index": 0,
                "checks": [],
                "observations": [],
                "pending": None,
                "model_pending": False,
                "turns": 0,
                "amendments": [],
                "review": None,
                "setup_retry": False,
            },
        )
        if session["binding"] != binding:
            raise BackendError("Frozen implementation inputs changed.")
        if session["phase"] == "failed":
            return ImplementationResult("failed", {"reason": session["failure"]})
        if session["plan_id"] != plan["id"] or session["plan_digest"] != plan["digest"]:
            if session["model_pending"] or (
                session["pending"] and session["pending"].get("started")
            ):
                raise BackendError(
                    "Resolve uncertain execution before changing the saved plan."
                )
            reason = plan.get("reason")
            if not isinstance(reason, str) or not reason.strip():
                raise BackendError("Material plan amendments require a saved reason.")
            session["amendments"].append(
                {"before": session["plan_id"], "after": plan["id"], "reason": reason}
            )
            session.update(
                {
                    "plan_id": plan["id"],
                    "plan_digest": plan["digest"],
                    "checks": [],
                    "index": 0,
                    "phase": "setup",
                    "pending": None,
                    "review": None,
                    "setup_retry": False,
                }
            )
        with workspace.locked():
            current = workspace_evidence(workspace)
        if session["phase"] == "delivery_ready":
            if session["review"] == current:
                return ImplementationResult(
                    "delivery_ready", self._delivery(session, context, current)
                )
            session.update(
                {"phase": "implementation", "checks": [], "index": 0, "review": None}
            )
        if session["phase"] in {"setup", "validation"}:
            return self._commands(state, session, document, workspace, context)
        if session["phase"] == "review":
            if current["changed"] == "no":
                session.update(
                    {
                        "phase": "failed",
                        "failure": "Implementation produced no changes; no PR can be delivered.",
                    }
                )
                self.journal.save(state)
                return ImplementationResult("failed", {"reason": session["failure"]})
            session.update(
                {"review": current, "phase": "validation", "index": 0, "checks": []}
            )
            self.journal.save(state)
            self.progress(BackendEvent("diff_reviewed", current))
            return ImplementationResult("continue", {"phase": "validation", **current})
        readiness = self.backend.readiness(request.account)
        caps = readiness.capabilities
        if (
            not readiness.ready
            or not readiness.authenticated
            or not caps.implementation
            or caps.native_tools
            or caps.transport != "structured_proposals"
        ):
            raise BackendError("Backend implementation readiness is unverified.")
        if session["pending"] is None:
            if session["model_pending"]:
                raise BackendError(
                    "Previous model outcome is uncertain; automatic replay is forbidden."
                )
            if session["turns"] >= 100:
                raise BackendError(
                    "Implementation exceeded its bounded model-turn limit."
                )
            prompt = {
                "stage": "implementation",
                "plan": plan,
                "context": context,
                "workspace": current,
                "files": checkout_inventory(workspace),
                "observations": session["observations"],
            }
            if len(canonical_bytes(prompt)) > 1024 * 1024:
                raise BackendError("Implementation context exceeds its bounded size.")
            session["model_pending"] = True
            self.journal.save(state)
            event = self.budget.run(
                "implementation",
                lambda remaining: self.backend.implementation(
                    replace(
                        request,
                        document=prompt,
                        timeout_seconds=min(request.timeout_seconds, remaining),
                    ),
                    progress=self.progress,
                    checkpoint=self._periodic,
                    stop=self.stop,
                ),
            )
            session["pending"] = {
                "kind": event.kind,
                "data": event.data,
                "operation_id": str(uuid4()),
            }
            session["model_pending"] = False
            session["turns"] += 1
            self.journal.save(state)
        if not self._allowed():
            return ImplementationResult(
                "wait", {"reason": "Checkpoint closed before applying proposal."}
            )
        return self._proposal(state, session, plan, context, workspace)

    def _proposal(self, state, session, plan, context, workspace):
        from .codex_backend import validate_proposal

        pending = session["pending"]
        kind, data = pending["kind"], pending["data"]
        validate_proposal(kind, data)
        if pending.get("started"):
            raise BackendError(
                "Previous workspace action is uncertain; recovery is required."
            )
        if kind == "read":
            with workspace.locked():
                workspace.verify()
                raw = read_checkout(workspace, data["path"])
            observation = {
                "path": data["path"],
                "content": raw.decode("utf-8"),
                "digest": hashlib.sha256(raw).hexdigest(),
            }
            session["observations"].append(observation)
        elif kind == "patch":
            pending["started"] = True
            self.journal.save(state)
            with workspace.locked():
                apply_patch(workspace, data)
            session["checks"] = []
            session["review"] = None
        elif kind == "action":
            return self._execute(
                state,
                session,
                ActionSpec.from_data(data["action"]),
                workspace,
                "implementation",
            )
        elif kind == "complete":
            session["phase"] = "setup" if session.get("setup_retry") else "review"
        elif kind == "amendment":
            validate_plan(
                data["document"],
                context["document"],
                context["id"],
                workspace.base_revision,
            )
            self.store.save_plan(
                data["document"],
                context,
                reason=data["reason"],
                operation_id=pending["operation_id"],
            )
        else:
            raise BackendOutputError(
                "Implementation proposal cannot publish or commit."
            )
        session["pending"] = None
        self.journal.save(state)
        return ImplementationResult(
            "continue", {"phase": session["phase"], "proposal": kind}
        )

    def _commands(self, state, session, document, workspace, context):
        phase = session["phase"]
        actions = list(
            document["setup_actions" if phase == "setup" else "validation_actions"]
        )
        key = "setup_commands" if phase == "setup" else "validation_commands"
        commands = self.snapshot["effective"][key]
        reference = "run-configuration:" + workspace.workspace_id
        source = self.sources.register(
            "operator", reference, canonical_bytes(self.snapshot)
        )
        for command in commands:
            actions.append(
                ActionSpec(
                    ("/bin/sh", "-c", command),
                    "shell",
                    "operator",
                    reference,
                    source,
                    workspace.workspace_id,
                    workspace.base_revision,
                    workspace.verify(),
                ).to_data()
            )
        if phase == "validation" and not actions:
            raise BackendError("At least one required validation action must pass.")
        if session["index"] == len(actions):
            if phase == "setup":
                next_phase = (
                    "review" if session.get("setup_retry") else "implementation"
                )
                session.update({"phase": next_phase, "index": 0, "setup_retry": False})
                self.journal.save(state)
                return ImplementationResult("continue", {"phase": next_phase})
            with workspace.locked():
                current = workspace_evidence(workspace)
            if current != session["review"]:
                session.update({"phase": "review", "index": 0, "checks": []})
                self.journal.save(state)
                return ImplementationResult(
                    "continue",
                    {
                        "reason": "Changed workspace requires diff review and all checks again."
                    },
                )
            session["phase"] = "delivery_ready"
            self.journal.save(state)
            return ImplementationResult(
                "delivery_ready", self._delivery(session, context, current)
            )
        with workspace.locked():
            original = ActionSpec.from_data(actions[session["index"]])
            action = replace(
                original,
                workspace_id=workspace.workspace_id,
                base_revision=workspace.base_revision,
                content_digest=workspace.verify(),
                source_digest=self.sources.current_digest(
                    original.source_kind, original.source_reference, workspace
                ),
            )
        if session["pending"] is None:
            session["pending"] = {
                "kind": "action",
                "data": {"action": action.to_data()},
                "operation_id": str(uuid4()),
            }
            self.journal.save(state)
        elif session["pending"]["data"]["action"] != action.to_data():
            if session["pending"].get("started"):
                raise BackendError(
                    "Interrupted command requires recovery before another execution."
                )
            session["pending"] = {
                "kind": "action",
                "data": {"action": action.to_data()},
                "operation_id": str(uuid4()),
            }
            self.journal.save(state)
        return self._execute(state, session, action, workspace, phase)

    def _execute(self, state, session, action, workspace, phase):
        pending = session["pending"]
        if pending.get("started"):
            raise BackendError(
                "Previous approved execution is uncertain; automatic replay is forbidden."
            )
        with workspace.locked():
            if (
                action.workspace_id != workspace.workspace_id
                or action.base_revision != workspace.base_revision
                or action.content_digest != workspace.verify()
                or action.source_digest
                != self.sources.current_digest(
                    action.source_kind, action.source_reference, workspace
                )
            ):
                raise BackendError(
                    "Proposed action must bind the current source and workspace."
                )
        approval = self.executor.broker.request(action, pending["operation_id"])
        if approval["decision"] != "approved":
            return ImplementationResult(
                "approval",
                {"approval_id": approval["id"], "decision": approval["decision"]},
            )
        if not self._allowed():
            return ImplementationResult(
                "wait", {"reason": "Execution checkpoint is closed."}
            )
        with workspace.locked():
            before = workspace_evidence(workspace)
        pending["started"] = True
        self.journal.save(state)
        stage = "validation" if phase == "validation" else "implementation"
        result = self.budget.run(
            stage,
            lambda remaining: self.executor.execute(
                workspace, action, approval["id"], timeout=min(86400, remaining)
            ),
        )
        with workspace.locked():
            after = workspace_evidence(workspace)
        evidence = {
            "phase": phase,
            "action": action.to_data(),
            "execution_id": result.execution_id,
            "exit_code": result.exit_code,
            "interrupted": result.interrupted,
            "before": before,
            "after": after,
            "output_digest": hashlib.sha256(result.output).hexdigest(),
            "output_bytes": len(result.output),
        }
        session["observations"].append(evidence)
        session["pending"] = None
        if result.interrupted:
            self.journal.save(state)
            return ImplementationResult(
                "wait",
                {
                    "reason": "Approved command was interrupted; fresh approval is required."
                },
            )
        if result.exit_code:
            if phase == "setup":
                session["setup_retry"] = True
            session.update(
                {"phase": "implementation", "index": 0, "checks": [], "review": None}
            )
        elif phase == "validation":
            if before != after:
                session.update(
                    {"phase": "review", "index": 0, "checks": [], "review": None}
                )
            else:
                session["checks"].append(evidence)
                session["index"] += 1
        elif phase == "setup":
            session["index"] += 1
        else:
            session["checks"] = []
            session["review"] = None
        self.journal.save(state)
        self.progress(
            BackendEvent(
                "validation_result" if phase == "validation" else "action_result",
                evidence,
            )
        )
        return ImplementationResult(
            "continue", {"phase": session["phase"], "exit_code": result.exit_code}
        )

    def _delivery(self, session, context, current):
        return {
            "schema_version": 1,
            "plan_id": session["plan_id"],
            "plan_digest": session["plan_digest"],
            "context_id": context["id"],
            "workspace": current,
            "validation": session["checks"],
            "amendments": session["amendments"],
        }
