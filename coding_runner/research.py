import os
import stat
import threading
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
from uuid import uuid4

from .approvals import ActionSpec, ApprovalRequired
from .backends import (
    Backend,
    BackendError,
    BackendEvent,
    BackendInterrupted,
    BackendOutputError,
    BackendRequest,
    Checkpoint,
    Progress,
    StageBudget,
)
from .context import canonical_bytes, digest
from .journal import Journal
from .plans import validate_context, validate_plan
from .workspaces import SourceStore, Workspace

MAX_FILE_BYTES = 128 * 1024
MAX_RESEARCH_BYTES = 1024 * 1024


def plan_requirements(context: dict[str, Any], workspace: Workspace) -> dict[str, Any]:
    return {
        "fixed": {
            "schema_version": 1,
            "context_id": context["id"],
            "context_digest": context["digest"],
            "original_issue": (
                context["document"]["issue"]["url"]
                if context["document"]["issue"]
                else None
            ),
            "checkout": context["document"]["checkout"],
            "inspected_base_commit": workspace.base_revision,
            "resolved_base_branch": workspace.resolved_base_branch,
        },
        "required_text": ["problem", "expected_behavior", "approach"],
        "required_nonempty_string_lists": [
            "acceptance_criteria",
            "steps",
            "file_changes",
            "delivery_evidence",
        ],
        "required_string_lists": [
            "findings",
            "relevant_paths",
            "tradeoffs",
            "regression_cases",
            "blockers",
            "open_questions",
        ],
        "sources": "List {kind,reference,digest} from supervisor observations and context sources. Include inspected repository code.",
        "setup_actions": "List complete ActionSpec objects; may be empty.",
        "validation_actions": "Nonempty list of complete ActionSpec objects binding listed sources and current workspace.",
        "action_fields": list(ActionSpec.__dataclass_fields__),
        "no_additional_fields": True,
    }


@dataclass(frozen=True)
class ResearchResult:
    status: str
    detail: dict[str, Any]


def read_checkout(workspace: Workspace, relative: Any) -> bytes:
    if not isinstance(relative, str) or not relative or relative.startswith("/"):
        raise BackendOutputError("Research reads require a relative checkout file.")
    parts = relative.split("/")
    if any(part in {"", ".", "..", ".git"} for part in parts):
        raise BackendOutputError("Research path escapes visible checkout content.")
    descriptor = os.open(
        workspace.checkout, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    )
    try:
        for part in parts[:-1]:
            child = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor
            )
            os.close(descriptor)
            descriptor = child
        file_descriptor = os.open(
            parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor
        )
        with os.fdopen(file_descriptor, "rb") as stream:
            details = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(details.st_mode)
                or details.st_nlink != 1
                or details.st_size > MAX_FILE_BYTES
            ):
                raise BackendOutputError(
                    "Research reads require a bounded regular file."
                )
            content = stream.read(MAX_FILE_BYTES + 1)
            if len(content) > MAX_FILE_BYTES:
                raise BackendOutputError("Research file exceeds the size limit.")
            return content
    except OSError:
        raise BackendOutputError("Research file could not be read safely.") from None
    finally:
        os.close(descriptor)


def checkout_inventory(workspace: Workspace) -> list[str]:
    paths = []
    for directory, subdirectories, files in os.walk(
        workspace.checkout, followlinks=False
    ):
        subdirectories[:] = [
            name
            for name in subdirectories
            if name != ".git" and not (Path(directory) / name).is_symlink()
        ]
        for name in sorted(files):
            path = Path(directory) / name
            if path.is_symlink():
                continue
            paths.append(path.relative_to(workspace.checkout).as_posix())
            if len(paths) > 10000:
                raise BackendError("Checkout inventory exceeds research limits.")
    return sorted(paths)


class ResearchStage:
    def __init__(
        self,
        backend: Backend,
        journal: Journal,
        store: Any,
        executor: Any,
        sources: SourceStore,
        *,
        checkpoint: Checkpoint,
        progress: Progress,
        stop: threading.Event | None = None,
        budget: StageBudget | None = None,
    ):
        if getattr(executor, "read_only", False) is not True:
            raise BackendError(
                "Research requires an executor with a read-only checkout."
            )
        self.backend = backend
        self.journal = journal
        self.store = store
        self.executor = executor
        self.sources = sources
        self.checkpoint = checkpoint
        self.budget = budget
        self.progress = progress
        self.stop = stop or threading.Event()

    def _checkpoint(self) -> bool:
        return self.checkpoint() is True and (
            self.budget is None or self.budget.checkpoint() is True
        )

    def step(
        self,
        state: dict[str, Any],
        run_id: str,
        item: dict[str, Any],
        context: dict[str, Any],
        workspace: Workspace,
        request: BackendRequest,
    ) -> ResearchResult:
        if self.stop.is_set() or self._checkpoint() is not True:
            self.backend.interrupt()
            raise BackendInterrupted("Research is waiting at a control checkpoint.")
        readiness = self.backend.readiness(request.account)
        capabilities = readiness.capabilities
        if (
            not readiness.ready
            or not readiness.authenticated
            or not capabilities.research
            or capabilities.native_tools
            or capabilities.transport != "structured_proposals"
        ):
            raise BackendError("Backend research readiness is unverified.")
        validate_context(context["document"], item)
        if (
            context["digest"] != digest(context["document"])
            or context["inspected_base_commit"] != workspace.base_revision
            or run_id != workspace.workspace_id
            or context["resolved_base_branch"] != workspace.resolved_base_branch
        ):
            raise BackendOutputError(
                "Saved context and inspected checkout do not match."
            )
        binding = digest(
            {
                "context": context["id"],
                "digest": context["digest"],
                "base": workspace.base_revision,
                "account": request.account,
                "model": request.model,
                "instructions": request.instructions,
            }
        )
        sessions = state.setdefault("research", {})
        session = sessions.setdefault(
            run_id,
            {
                "binding": binding,
                "observations": [],
                "pending": None,
                "steps": 0,
                "plan": None,
                "model_pending": False,
            },
        )
        if session["binding"] != binding:
            raise BackendError(
                "Research inputs changed; explicit reassessment is required."
            )
        if session["plan"] is not None:
            return ResearchResult("saved", session["plan"])
        sources = context.get("sources")
        if not isinstance(sources, list) or not sources:
            raise BackendError(
                "Supervisor-fetched context source bindings are required."
            )
        for source in sources:
            actual_source = self.sources.current_digest(
                source["kind"], source["reference"], workspace
            )
            if actual_source != source["digest"]:
                raise ApprovalRequired(
                    "Context source changed; refresh context before research."
                )
        with workspace.locked():
            actual = workspace.verify()
            inventory = checkout_inventory(workspace)
        inspected_digest = session.setdefault("workspace_digest", actual)
        if inspected_digest != actual:
            raise ApprovalRequired(
                "Research checkout changed; explicit reassessment is required."
            )
        if session["pending"] is None:
            if session["model_pending"]:
                raise BackendError(
                    "A previous model request has an unresolved outcome; operator recovery is required."
                )
            if session["steps"] >= 100:
                raise BackendError("Research exceeded its bounded model-turn limit.")
            document = {
                "stage": "research",
                "context": context,
                "workspace": {
                    "id": workspace.workspace_id,
                    "base_revision": workspace.base_revision,
                    "content_digest": actual,
                },
                "files": inventory,
                "sources": sources,
                "observations": session["observations"],
                "plan_requirements": plan_requirements(context, workspace),
            }
            if len(canonical_bytes(document)) > MAX_RESEARCH_BYTES:
                raise BackendError("Research exceeded the bounded context budget.")
            session["model_pending"] = True
            self.journal.save(state)

            def invoke_model(remaining: float) -> BackendEvent:
                bounded = replace(
                    request,
                    document=document,
                    timeout_seconds=min(request.timeout_seconds, remaining),
                )
                return self.backend.research(
                    bounded,
                    progress=self.progress,
                    checkpoint=self._checkpoint,
                    stop=self.stop,
                )

            event = (
                self.budget.run("research", invoke_model)
                if self.budget is not None
                else invoke_model(request.timeout_seconds)
            )
            session["pending"] = {
                "kind": event.kind,
                "data": event.data,
                "operation_id": str(uuid4()),
            }
            session["steps"] += 1
            session["model_pending"] = False
            self.journal.save(state)
        pending = session["pending"]
        if not isinstance(pending, dict):
            raise BackendError("Research proposal state is invalid.")
        result = self._apply(pending, session, context, workspace, state)
        if result.status != "approval":
            session["pending"] = None
        self.journal.save(state)
        return result

    def _apply(
        self,
        pending: dict[str, Any],
        session: dict[str, Any],
        context: dict[str, Any],
        workspace: Workspace,
        state: dict[str, Any],
    ) -> ResearchResult:
        kind, data = pending["kind"], pending["data"]
        if self.stop.is_set() or self._checkpoint() is not True:
            raise BackendInterrupted("Research interrupted before applying a proposal.")
        if kind == "read" and set(data) == {"path"}:
            with workspace.locked():
                workspace.verify()
                raw = read_checkout(workspace, data["path"])
                source_digest = self.sources.register("repository", data["path"], raw)
            try:
                content = raw.decode("utf-8")
            except UnicodeError:
                raise BackendOutputError("Research file is not UTF-8 text.") from None
            session["observations"].append(
                {
                    "kind": "repository",
                    "reference": data["path"],
                    "digest": source_digest,
                    "content": content,
                }
            )
            return ResearchResult("continue", {"read": data["path"]})
        if kind == "action" and set(data) == {"action"}:
            action = ActionSpec.from_data(data["action"])
            if pending.get("execution_started"):
                raise BackendError(
                    "A previous approved action needs operator recovery; it will not be repeated."
                )
            with workspace.locked():
                actual = workspace.verify()
                action_source = self.sources.current_digest(
                    action.source_kind, action.source_reference, workspace
                )
                if (
                    action.workspace_id != workspace.workspace_id
                    or action.base_revision != workspace.base_revision
                    or action.content_digest != actual
                    or action.source_digest != action_source
                ):
                    raise ApprovalRequired(
                        "Action must bind the current supervisor-verified workspace and source."
                    )
            approval = self.executor.broker.request(action, pending["operation_id"])
            if approval["decision"] != "approved":
                return ResearchResult(
                    "approval",
                    {"approval_id": approval["id"], "decision": approval["decision"]},
                )
            pending["execution_started"] = True
            self.journal.save(state)

            def invoke_action(remaining: float) -> Any:
                return self.executor.execute(
                    workspace, action, approval["id"], timeout=min(60, remaining)
                )

            result = (
                self.budget.run("research", invoke_action)
                if self.budget is not None
                else invoke_action(60)
            )
            if result.interrupted:
                raise BackendInterrupted("Approved research action was interrupted.")
            raw = result.output
            reference = "approved-execution:" + result.execution_id
            source_digest = self.sources.register("research", reference, raw)
            session["observations"].append(
                {
                    "kind": "research",
                    "reference": reference,
                    "digest": source_digest,
                    "content": raw.decode("utf-8", errors="replace"),
                    "exit_code": result.exit_code,
                }
            )
            return ResearchResult(
                "continue",
                {"execution_id": result.execution_id, "exit_code": result.exit_code},
            )
        if kind == "plan" and set(data) == {"document"}:
            document = data["document"]
            if not any(
                entry["kind"] == "repository" for entry in session["observations"]
            ):
                raise BackendOutputError(
                    "Inspect checkout code before submitting a research plan."
                )
            validate_plan(
                document, context["document"], context["id"], workspace.base_revision
            )
            if document["resolved_base_branch"] != workspace.resolved_base_branch:
                raise BackendOutputError(
                    "Plan resolved base branch does not match the checkout."
                )
            inspected = {
                (entry["reference"], entry["digest"])
                for entry in session["observations"]
                if entry["kind"] == "repository"
            }
            listed = {
                (source["reference"], source["digest"])
                for source in document["sources"]
                if source["kind"] == "repository"
            }
            if not inspected.intersection(listed):
                raise BackendOutputError(
                    "Plan must cite supervisor-inspected checkout code."
                )
            with workspace.locked():
                if workspace.verify() != session["workspace_digest"]:
                    raise ApprovalRequired(
                        "Research checkout changed before plan persistence."
                    )
                for source in document["sources"]:
                    actual = self.sources.current_digest(
                        source["kind"], source["reference"], workspace
                    )
                    if actual != source["digest"]:
                        raise ApprovalRequired(
                            "Plan source changed; reassessment is required."
                        )
            saved = self.store.save_plan(
                document,
                context,
                reason="Research inspected checkout and validated structured plan",
                operation_id=pending["operation_id"],
            )
            session["plan"] = saved
            self.progress(BackendEvent("plan_saved", {"plan_id": saved["id"]}))
            return ResearchResult("saved", saved)
        raise BackendOutputError(
            "Research proposal cannot implement, patch or publish."
        )
