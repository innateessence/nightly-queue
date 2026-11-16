"""Trusted merge configuration and provider receipt identities."""

from .native_contract import (
    NativeContractError,
    checked,
    copied,
    fields,
    positive,
    sha,
    text,
)


@checked
def merge_profile(value):
    fields(value, {"schema_version", "repository_id", "actor_id", "checks", "reviews", "ci_timeout_seconds", "delete_branch"})
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise NativeContractError("Unsupported merge profile.")
    positive(value["repository_id"])
    positive(value["actor_id"])
    timeout = value["ci_timeout_seconds"]
    if type(timeout) is not int or not 1 <= timeout <= 86400:
        raise NativeContractError("CI wait must be between1 and86400 seconds.")
    if type(value["delete_branch"]) is not bool:
        raise NativeContractError("Branch cleanup must be explicit.")
    checks = value["checks"]
    if not isinstance(checks, list) or not 1 <= len(checks) <= 32:
        raise NativeContractError("Automatic merge requires a nonempty trusted check set.")
    for entry in checks:
        fields(entry, {"name", "app_id", "workflow_id", "workflow_path", "workflow_digest"})
        text(entry["name"], 200)
        positive(entry["app_id"])
        positive(entry["workflow_id"])
        path = entry["workflow_path"]
        text(path, 200)
        if not path.startswith(".github/workflows/") or ".." in path or "@" in path:
            raise NativeContractError("Required workflows must use a fixed repository path.")
        sha(entry["workflow_digest"])
    names = [entry["name"] for entry in checks]
    if len(set(names)) != len(names):
        raise NativeContractError("Required check names must be unique.")
    reviews = value["reviews"]
    if not isinstance(reviews, list) or len(reviews) != 2:
        raise NativeContractError("Configure independent and root AI review producers.")
    for entry in reviews:
        fields(entry, {"role", "context", "creator_id", "report_repository", "report_repository_id", "report_prefix"})
        positive(entry["creator_id"])
        positive(entry["report_repository_id"])
        for key in ("context", "report_repository", "report_prefix"):
            text(entry[key], 200)
        if entry["report_prefix"].startswith("/") or ".." in entry["report_prefix"]:
            raise NativeContractError("Review report prefix must be repository relative.")
    if {entry["role"] for entry in reviews} != {"independent", "root"}:
        raise NativeContractError("Both independent and root AI review are required.")
    if len({entry["context"] for entry in reviews}) != 2:
        raise NativeContractError("Review contexts must be distinct.")
    return copied(value)


def configured_profile(value):
    return {} if value == {} else merge_profile(value)


@checked
def review_report(value, *, role, run_id, context_digest, workspace, pull_number):
    fields(value, {"schema_version", "role", "run_id", "context_digest", "repository_id", "pull_number", "head_sha", "base_sha", "tree_sha", "session_id", "reviewer", "findings", "verdict"})
    expected = {
        "schema_version": 1,
        "role": role,
        "run_id": run_id,
        "context_digest": context_digest,
        "repository_id": workspace["repository_id"],
        "pull_number": pull_number,
        "head_sha": workspace["commit_sha"],
        "base_sha": workspace["base_revision"],
        "tree_sha": workspace["tree_sha"],
        "verdict": "no_p2_or_higher",
    }
    if any(value[key] != entry for key, entry in expected.items()):
        raise NativeContractError("Review report does not authorize this exact candidate.")
    text(value["session_id"], 200)
    text(value["reviewer"], 200)
    findings = value["findings"]
    if not isinstance(findings, list) or len(findings) > 100:
        raise NativeContractError("Review findings must be bounded.")
    for finding in findings:
        fields(finding, {"priority", "description"})
        if finding["priority"] not in {"P3", "P4"}:
            raise NativeContractError("Review has unresolved P2 or higher findings.")
        text(finding["description"], 2000)
    return copied(value)
