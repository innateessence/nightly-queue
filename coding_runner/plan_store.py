from typing import Any
from uuid import UUID, uuid4

from .context import GitHubContextClient, digest, manual_context
from .plans import register_context_sources, validate_context, validate_plan


class ContextPlanStore:
    def __init__(self, client: Any, run_id: str, claim_token: str, sources: Any):
        self.client = client
        self.run_id = str(UUID(run_id))
        self.claim_token = claim_token
        self.sources = sources

    def _post(
        self, endpoint: str, payload: dict[str, Any], operation_id: str | None
    ) -> dict[str, Any]:
        body = {
            "claim_token": self.claim_token,
            "operation_id": str(UUID(operation_id)) if operation_id else str(uuid4()),
            **payload,
        }
        return self.client.request(
            "POST", f"/api/v1/runner/runs/{self.run_id}/{endpoint}/", body
        )

    def refresh(
        self,
        item: dict[str, Any],
        base_commit: str,
        *,
        issue_client: GitHubContextClient | None = None,
        resolved_base_branch: str | None = None,
        reason: str = "Refresh discussion and inspect base before handoff",
        operation_id: str | None = None,
    ) -> dict[str, Any]:
        if item.get("source") == "manual":
            document = manual_context(item)
        elif issue_client is not None:
            document = issue_client.fetch(item)
        else:
            raise ValueError(
                "Complete current issue context is required; no issue client is configured."
            )
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
        )["context"]
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
        return self._post(
            "plans", {"document": document, "reason": reason}, operation_id
        )["plan"]

    def handoff(
        self,
        item: dict[str, Any],
        base_commit: str,
        *,
        issue_client: GitHubContextClient | None = None,
        resolved_base_branch: str | None = None,
        operation_id: str | None = None,
    ) -> dict[str, Any]:
        # Refresh failure propagates; an older empty or complete snapshot is never substituted.
        current = self.refresh(
            item,
            base_commit,
            issue_client=issue_client,
            resolved_base_branch=resolved_base_branch,
        )
        return self._post(
            "handoff",
            {"context_id": current["id"], "base_commit": base_commit},
            operation_id,
        )
