"""Version 2 receipt shape; server ownership and delivery checks are separate."""

from datetime import datetime
import re
from typing import Any
from uuid import UUID


ROOT_FIELDS = {
    "schema_version",
    "execution_kind",
    "run_id",
    "operation_id",
    "runner_id",
    "configuration_revision",
    "policy_digest",
    "context",
    "runtime",
    "workspace",
    "validation",
    "delivery",
}


def native_evidence_contract() -> dict[str, Any]:
    return {
        "schema_version": 2,
        "execution_kind": "native",
        "required_fields": sorted(ROOT_FIELDS),
    }


class EvidenceError(ValueError):
    pass


def fields(value, expected):
    if not isinstance(value, dict) or set(value) != set(expected):
        raise EvidenceError("Native delivery evidence has missing or unknown fields.")


def text(value):
    if not isinstance(value, str) or not value or len(value) > 2000 or "\x00" in value:
        raise EvidenceError("Native evidence requires bounded nonempty text.")
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise EvidenceError("Native evidence requires valid Unicode.") from None


def identifier(value):
    if not isinstance(value, str):
        raise EvidenceError("Native evidence requires UUID identifiers.")
    try:
        UUID(value)
    except ValueError:
        raise EvidenceError("Native evidence requires UUID identifiers.") from None


def digest(value, *, git=False, image=False):
    pattern = r"[0-9a-f]{40}|[0-9a-f]{64}" if git else r"[0-9a-f]{64}"
    if image:
        pattern = r"sha256:[0-9a-f]{64}"
    if not isinstance(value, str) or not re.fullmatch(pattern, value):
        raise EvidenceError("Native evidence has an invalid content digest.")


def positive(value):
    if type(value) is not int or value < 1:
        raise EvidenceError("Native evidence requires positive numeric identities.")


def validate_native_evidence(value: Any) -> dict[str, Any]:
    fields(value, ROOT_FIELDS)
    if (
        type(value["schema_version"]) is not int
        or value["schema_version"] != 2
        or value["execution_kind"] != "native"
    ):
        raise EvidenceError("Only version 2 native evidence is supported.")
    for key in ("run_id", "operation_id"):
        identifier(value[key])
    positive(value["runner_id"])
    for key in ("configuration_revision", "policy_digest"):
        digest(value[key])
    context = value["context"]
    fields(context, {"id", "digest"})
    identifier(context["id"])
    digest(context["digest"])
    runtime = value["runtime"]
    fields(runtime, {"image_digest", "profile_id", "agent_version", "session_id"})
    digest(runtime["image_digest"], image=True)
    for key in ("profile_id", "agent_version", "session_id"):
        text(runtime[key])
    workspace = value["workspace"]
    fields(
        workspace,
        {
            "repository_id",
            "repository",
            "branch",
            "base_branch",
            "base_revision",
            "tree_sha",
            "commit_sha",
        },
    )
    positive(workspace["repository_id"])
    for key in ("repository", "branch", "base_branch"):
        text(workspace[key])
    if workspace["branch"] == workspace["base_branch"]:
        raise EvidenceError("A task cannot deliver to its base branch directly.")
    for key in ("base_revision", "tree_sha", "commit_sha"):
        digest(workspace[key], git=True)
    validation = value["validation"]
    fields(
        validation,
        {"profile_id", "profile_digest", "image_digest", "tree_sha", "checks"},
    )
    text(validation["profile_id"])
    digest(validation["profile_digest"])
    digest(validation["image_digest"], image=True)
    digest(validation["tree_sha"], git=True)
    if validation["tree_sha"] != workspace["tree_sha"]:
        raise EvidenceError("The delivered tree must be the validated tree.")
    checks = validation["checks"]
    if not isinstance(checks, list) or not 1 <= len(checks) <= 128:
        raise EvidenceError("Native evidence requires completed validation checks.")
    identities = set()
    operations = set()
    for check in checks:
        fields(
            check,
            {
                "id",
                "operation_id",
                "exit_code",
                "timed_out",
                "started_at",
                "finished_at",
                "log_reference",
                "log_digest",
            },
        )
        for key in ("id", "log_reference"):
            text(check[key])
        identifier(check["operation_id"])
        if check["id"] in identities:
            raise EvidenceError("Validation command identities must be unique.")
        identities.add(check["id"])
        if check["operation_id"] in operations:
            raise EvidenceError("Validation execution operations must be unique.")
        operations.add(check["operation_id"])
        if (
            type(check["exit_code"]) is not int
            or check["exit_code"] != 0
            or check["timed_out"] is not False
        ):
            raise EvidenceError("Native delivery requires successful validation.")
        try:
            start = datetime.fromisoformat(check["started_at"])
            end = datetime.fromisoformat(check["finished_at"])
            if start.utcoffset() is None or end.utcoffset() is None or start > end:
                raise ValueError
        except (TypeError, ValueError):
            raise EvidenceError(
                "Validation requires ordered timezone-aware timestamps."
            ) from None
        digest(check["log_digest"])
    delivery = value["delivery"]
    fields(
        delivery,
        {"mode", "pull_request", "pushed_sha", "checks", "merge", "issue_completion"},
    )
    if delivery["mode"] not in ("pull_request", "merge"):
        raise EvidenceError("Native evidence has an invalid delivery mode.")
    digest(delivery["pushed_sha"], git=True)
    if delivery["pushed_sha"] != workspace["commit_sha"]:
        raise EvidenceError("The pushed commit must be the validated commit.")
    pull = delivery["pull_request"]
    fields(pull, {"repository_id", "number", "url", "base", "head", "head_sha"})
    positive(pull["repository_id"])
    positive(pull["number"])
    expected = f"https://github.com/{workspace['repository']}/pull/{pull['number']}"
    if pull["url"] != expected or pull["repository_id"] != workspace["repository_id"]:
        raise EvidenceError("The pull request must belong to the claimed repository.")
    if (
        pull["base"] != workspace["base_branch"]
        or pull["head"] != workspace["branch"]
        or pull["head_sha"] != workspace["commit_sha"]
    ):
        raise EvidenceError(
            "The pull request must identify the delivered branch and commit."
        )
    if not isinstance(delivery["checks"], list) or len(delivery["checks"]) > 128:
        raise EvidenceError("Native remote checks must be a bounded list.")
    for check in delivery["checks"]:
        fields(check, {"name", "producer_id", "head_sha", "conclusion", "url"})
        text(check["name"])
        text(check["url"])
        positive(check["producer_id"])
        if (
            check["head_sha"] != workspace["commit_sha"]
            or check["conclusion"] != "success"
        ):
            raise EvidenceError("Remote checks must pass for the delivered commit.")
    if delivery["mode"] == "pull_request":
        if delivery["merge"] is not None or delivery["issue_completion"] is not None:
            raise EvidenceError(
                "PR-only evidence cannot claim a merge or issue completion."
            )
        return value
    merge = delivery["merge"]
    fields(merge, {"commit_sha", "head_sha"})
    digest(merge["commit_sha"], git=True)
    if merge["head_sha"] != workspace["commit_sha"] or not delivery["checks"]:
        raise EvidenceError("Merge evidence requires exact-head remote checks.")
    completion = delivery["issue_completion"]
    if completion is not None:
        fields(completion, {"operation_id", "issue_number", "state"})
        identifier(completion["operation_id"])
        positive(completion["issue_number"])
        if completion["state"] != "closed":
            raise EvidenceError("Issue completion must record closure.")
    return value
