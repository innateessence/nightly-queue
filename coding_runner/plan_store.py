import time
from typing import Any, Callable
from uuid import UUID, uuid4

from .client import NotFoundError
from .context import GitHubContextClient, check_deadline, digest, manual_context
from .plans import register_context_sources, validate_context, validate_plan


class ContextPlanStore:
    def __init__(self, client: Any, run_id: str, claim_token: str, sources: Any):
        self.client = client
        self.run_id = str(UUID(run_id))
        self.claim_token = claim_token
        self.sources = sources

    def _post(
        self,
        endpoint: str,
        payload: dict[str, Any],
        operation_id: str | None,
        *,
        deadline: float | None = None,
        checkpoint: Callable[[], bool] | None = None,
        inflight_checkpoint: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        body = {
            "claim_token": self.claim_token,
            "operation_id": str(UUID(operation_id)) if operation_id else str(uuid4()),
            **payload,
        }
        remaining = check_deadline(deadline, checkpoint)
        limits: dict[str, Any] = {}
        if remaining is not None:
            limits["timeout"] = remaining
        if inflight_checkpoint is not None:
            limits["checkpoint"] = inflight_checkpoint
        result = self.client.request(
            "POST", f"/api/v1/runner/runs/{self.run_id}/{endpoint}/", body, **limits
        )
        check_deadline(deadline, checkpoint)
        return result

    def refresh(
        self,
        item: dict[str, Any],
        base_commit: str,
        *,
        issue_client: GitHubContextClient | None = None,
        resolved_base_branch: str | None = None,
        reason: str = "Refresh discussion and inspect base before handoff",
        operation_id: str | None = None,
        deadline: float | None = None,
        checkpoint: Callable[[], bool] | None = None,
        inflight_checkpoint: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        if item.get("source") == "manual":
            document = manual_context(item)
        elif issue_client is not None:
            document = issue_client.fetch(
                item,
                deadline=deadline,
                checkpoint=checkpoint,
                inflight_checkpoint=inflight_checkpoint,
            )
        else:
            raise ValueError(
                "Complete current issue context is required; no issue client is configured."
            )
        check_deadline(deadline, checkpoint)
        validate_context(document, item)
        resolved = resolved_base_branch or item.get("base_branch")
        if not resolved:
            raise ValueError("Record the supervisor-resolved remote default branch.")
        sources = register_context_sources(document, self.sources)
        saved = self._post(
            "contexts",
            {
                "document": document,
                "base_commit": base_commit,
                "resolved_base_branch": resolved,
                "reason": reason,
            },
            operation_id,
            deadline=deadline,
            checkpoint=checkpoint,
            inflight_checkpoint=inflight_checkpoint,
        )["context"]
        if saved.get("presentation_only") or saved.get("unavailable"):
            raise ValueError(
                "Saved context is only a redacted presentation and cannot be used."
            )
        if saved["digest"] != digest(saved["document"]):
            raise ValueError("Saved context digest does not match its document.")
        saved["sources"] = sources
        return saved

    def save_plan(
        self,
        document: dict[str, Any],
        context: dict[str, Any],
        *,
        reason: str,
        operation_id: str | None = None,
    ) -> dict[str, Any]:
        validate_plan(
            document,
            context["document"],
            context["id"],
            context["inspected_base_commit"],
        )
        saved = self._post(
            "plans", {"document": document, "reason": reason}, operation_id
        )["plan"]
        if saved.get("presentation_only") or saved.get("unavailable"):
            raise ValueError(
                "Saved plan is only a redacted presentation and cannot be used."
            )
        return saved

    def handoff(
        self,
        item: dict[str, Any],
        base_commit: str,
        *,
        issue_client: GitHubContextClient | None = None,
        resolved_base_branch: str | None = None,
        operation_id: str | None = None,
        timeout_seconds: float | None = None,
        checkpoint: Callable[[], bool] | None = None,
        inflight_checkpoint: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        deadline = (
            None if timeout_seconds is None else time.monotonic() + timeout_seconds
        )
        if operation_id:
            operation_id = str(UUID(operation_id))
            path = f"/api/v1/runner/runs/{self.run_id}/handoff/{operation_id}/"
            try:
                remaining = check_deadline(deadline, checkpoint)
                limits: dict[str, Any] = {}
                if remaining is not None:
                    limits["timeout"] = remaining
                if inflight_checkpoint is not None:
                    limits["checkpoint"] = inflight_checkpoint
                previous = self.client.request("GET", path, **limits)
                check_deadline(deadline, checkpoint)
                saved = previous["context"]
                documents = [saved, previous["plan"]]
                if any(
                    entry.get("presentation_only") or entry.get("unavailable")
                    for entry in documents
                ):
                    raise ValueError(
                        "Saved handoff is only a redacted presentation and cannot be used."
                    )
                validate_context(saved["document"], item)
                if saved["inspected_base_commit"] != base_commit:
                    raise ValueError(
                        "Handoff operation already inspected a different base."
                    )
                if (
                    resolved_base_branch
                    and saved["resolved_base_branch"] != resolved_base_branch
                ):
                    raise ValueError(
                        "Handoff operation already resolved a different base branch."
                    )
                return previous
            except NotFoundError:
                pass
        # Refresh failure propagates; an older empty or complete snapshot is never substituted.
        current = self.refresh(
            item,
            base_commit,
            issue_client=issue_client,
            resolved_base_branch=resolved_base_branch,
            deadline=deadline,
            checkpoint=checkpoint,
            inflight_checkpoint=inflight_checkpoint,
        )
        saved = self._post(
            "handoff",
            {"context_id": current["id"], "base_commit": base_commit},
            operation_id,
            deadline=deadline,
            checkpoint=checkpoint,
            inflight_checkpoint=inflight_checkpoint,
        )
        documents = [saved["context"], saved["plan"]]
        if any(
            entry.get("presentation_only") or entry.get("unavailable")
            for entry in documents
        ):
            raise ValueError(
                "Saved handoff is only a redacted presentation and cannot be used."
            )
        return saved
