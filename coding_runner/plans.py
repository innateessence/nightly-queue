"""Versioned, strictly structured handoff documents. A plan grants no permission."""

import re
from typing import Any
from uuid import UUID

from .context import ContextError, checkout, digest, repository_path, text, timestamp

SHA = r"[0-9a-f]{40}(?:[0-9a-f]{24})?"
DIGEST = r"[0-9a-f]{64}"


def exact(value: Any, keys: set[str], name: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise ContextError(f"Invalid {name} fields.")
    return value


def strings(value: Any, name: str, *, required: bool = False) -> list[str]:
    if not isinstance(value, list) or (required and not value):
        raise ContextError(f"Invalid {name}.")
    for entry in value:
        text(entry, name)
    return value


def revision(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(SHA, value):
        raise ContextError("Invalid inspected base commit.")
    return value


def validate_context(value: Any, item: dict[str, Any]) -> dict[str, Any]:
    keys = {
        "schema_version",
        "source",
        "title",
        "instructions",
        "issue",
        "comments",
        "fetched_at",
        "checkout",
    }
    context = exact(value, keys, "context")
    if type(context["schema_version"]) is not int or context["schema_version"] != 1:
        raise ContextError("Unsupported context schema.")
    if context["source"] != item["source"] or context["checkout"] != checkout(item):
        raise ContextError("Context does not match the frozen claim.")
    timestamp(context["fetched_at"])
    text(context["title"], "title")
    text(context["instructions"], "instructions", empty=True)
    if not isinstance(context["comments"], list):
        raise ContextError("Invalid comments.")
    if item["source"] == "manual":
        if (
            context["issue"] is not None
            or context["comments"]
            or context["title"] != item["title"]
            or context["instructions"] != item["instructions"]
        ):
            raise ContextError("Manual context must preserve claimed instructions.")
        return context
    issue = exact(
        context["issue"],
        {
            "number",
            "repository",
            "state",
            "body",
            "author",
            "created_at",
            "updated_at",
            "url",
        },
        "issue",
    )
    original = f"https://github.com{repository_path(item['issue_repository'])}/issues/{item['issue_number']}"
    if (
        issue["number"] != item["issue_number"]
        or issue["repository"] != item["issue_repository"]
        or issue["url"] != original
        or issue["state"] != "open"
    ):
        raise ContextError("Issue is closed or does not match the claim.")
    validate_entry(issue)
    if context["instructions"] != issue["body"]:
        raise ContextError("Context body does not match the current issue.")
    seen = set()
    for comment in context["comments"]:
        exact(
            comment,
            {"id", "body", "author", "created_at", "updated_at", "url"},
            "comment",
        )
        if (
            type(comment["id"]) is not int
            or comment["id"] <= 0
            or comment["id"] in seen
        ):
            raise ContextError("Invalid or duplicate comment identity.")
        seen.add(comment["id"])
        if comment["url"] != original + f"#issuecomment-{comment['id']}":
            raise ContextError("Comment does not belong to the claimed issue.")
        validate_entry(comment)
    return context


def validate_entry(value: dict[str, Any]) -> None:
    text(value["body"], "body", empty=True)
    text(value["author"], "author")
    timestamp(value["created_at"])
    timestamp(value["updated_at"])


def validate_plan(
    value: Any, context: dict[str, Any], context_id: str, base_commit: str
) -> dict[str, Any]:
    from .approvals import ActionSpec

    keys = {
        "schema_version",
        "context_id",
        "context_digest",
        "problem",
        "expected_behavior",
        "acceptance_criteria",
        "findings",
        "sources",
        "relevant_paths",
        "approach",
        "tradeoffs",
        "steps",
        "file_changes",
        "setup_actions",
        "validation_actions",
        "regression_cases",
        "delivery_evidence",
        "blockers",
        "open_questions",
        "original_issue",
        "checkout",
        "inspected_base_commit",
    }
    plan = exact(value, keys, "plan")
    if type(plan["schema_version"]) is not int or plan["schema_version"] != 1:
        raise ContextError("Unsupported plan schema.")
    try:
        UUID(plan["context_id"])
    except (ValueError, TypeError, AttributeError):
        raise ContextError("Invalid context reference.") from None
    if plan["context_id"] != context_id or plan["context_digest"] != digest(context):
        raise ContextError("Plan must reference the saved context and its digest.")
    original = context["issue"]["url"] if context["issue"] else None
    if plan["original_issue"] != original or plan["checkout"] != context["checkout"]:
        raise ContextError("Plan must preserve issue and checkout identity.")
    if revision(plan["inspected_base_commit"]) != base_commit:
        raise ContextError("Plan base commit does not match its inspected context.")
    for key in ("problem", "expected_behavior", "approach"):
        text(plan[key], key)
    for key in ("acceptance_criteria", "steps", "file_changes", "delivery_evidence"):
        strings(plan[key], key, required=True)
    for key in (
        "findings",
        "relevant_paths",
        "tradeoffs",
        "regression_cases",
        "blockers",
        "open_questions",
    ):
        strings(plan[key], key)
    for path in [*plan["relevant_paths"], *plan["file_changes"]]:
        if path.startswith("/") or ".." in path.split("/") or "\x00" in path:
            raise ContextError("Plan paths must stay within the checkout.")
    if not isinstance(plan["sources"], list):
        raise ContextError("Invalid plan sources.")
    sources = set()
    for source in plan["sources"]:
        exact(source, {"kind", "reference", "digest"}, "source")
        if source["kind"] not in {
            "issue",
            "comment",
            "repository",
            "research",
            "plan",
            "operator",
        }:
            raise ContextError("Invalid source kind.")
        text(source["reference"], "source reference")
        if not isinstance(source["digest"], str) or not re.fullmatch(
            DIGEST, source["digest"]
        ):
            raise ContextError("Invalid source digest.")
        sources.add((source["kind"], source["reference"], source["digest"]))
    for key in ("setup_actions", "validation_actions"):
        if not isinstance(plan[key], list) or (
            key == "validation_actions" and not plan[key]
        ):
            raise ContextError(f"Invalid {key}.")
        for proposal in plan[key]:
            try:
                action = ActionSpec.from_data(proposal)
            except (ValueError, TypeError) as exc:
                raise ContextError("Invalid structured action proposal.") from exc
            source = (
                proposal["source_kind"],
                proposal["source_reference"],
                proposal["source_digest"],
            )
            if source not in sources or action.base_revision != base_commit:
                raise ContextError(
                    "Action must bind a listed source and inspected base."
                )
    return plan


def register_context_sources(
    context: dict[str, Any], store: Any
) -> list[dict[str, str]]:
    from .context import canonical_bytes

    sources = []
    if context["issue"] is None:
        entries = [("operator", "manual-task", context)]
    else:
        entries = [("issue", context["issue"]["url"], context["issue"])]
        entries.extend(
            ("comment", comment["url"], comment) for comment in context["comments"]
        )
    for kind, reference, content in entries:
        actual_digest = store.register(kind, reference, canonical_bytes(content))
        sources.append({"kind": kind, "reference": reference, "digest": actual_digest})
    return sources
