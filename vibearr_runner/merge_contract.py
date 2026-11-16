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
    if isinstance(value, dict):
        value = {"reviews": [], **value}
    fields(
        value,
        {
            "schema_version",
            "repository_id",
            "actor_id",
            "checks",
            "reviews",
            "ci_timeout_seconds",
            "delete_branch",
        },
    )
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
        raise NativeContractError(
            "Automatic merge requires a nonempty trusted check set."
        )
    for entry in checks:
        fields(
            entry, {"name", "app_id", "workflow_id", "workflow_path", "workflow_digest"}
        )
        text(entry["name"], 200)
        positive(entry["app_id"])
        positive(entry["workflow_id"])
        path = entry["workflow_path"]
        text(path, 200)
        if not path.startswith(".github/workflows/") or ".." in path or "@" in path:
            raise NativeContractError(
                "Required workflows must use a fixed repository path."
            )
        sha(entry["workflow_digest"])
    names = [entry["name"] for entry in checks]
    if len(set(names)) != len(names):
        raise NativeContractError("Required check names must be unique.")
    reviews = value["reviews"]
    if not isinstance(reviews, list) or len(reviews) not in {0, 2}:
        raise NativeContractError(
            "Reviews are optional; configure both producers or leave the list empty."
        )
    for entry in reviews:
        fields(
            entry,
            {
                "role",
                "context",
                "creator_id",
                "report_repository",
                "report_repository_id",
                "report_prefix",
            },
        )
        positive(entry["creator_id"])
        positive(entry["report_repository_id"])
        for key in ("context", "report_repository", "report_prefix"):
            text(entry[key], 200)
        if entry["report_prefix"].startswith("/") or ".." in entry["report_prefix"]:
            raise NativeContractError(
                "Review report prefix must be repository relative."
            )
    if reviews and {entry["role"] for entry in reviews} != {"independent", "root"}:
        raise NativeContractError("Both independent and root AI review are required.")
    if len({entry["context"] for entry in reviews}) != len(reviews):
        raise NativeContractError("Review contexts must be distinct.")
    return copied(value)


def configured_profile(value):
    return {} if value == {} else merge_profile(value)


@checked
def merge_receipt(value, profile, workspace):
    from .native_contract import digest

    fields(value, {"policy", "checks", "reviews"})
    policy = value["policy"]
    fields(policy, {"actor_id", "profile_digest", "rulesets"})
    positive(policy["actor_id"])
    sha(policy["profile_digest"])
    if (
        not isinstance(policy["rulesets"], list)
        or not 1 <= len(policy["rulesets"]) <= 100
    ):
        raise NativeContractError(
            "Merge requires effective enforced ruleset observations."
        )
    for entry in policy["rulesets"]:
        fields(entry, {"id", "digest"})
        positive(entry["id"])
        sha(entry["digest"])
    checks = value["checks"]
    if not isinstance(checks, list) or not 1 <= len(checks) <= 32:
        raise NativeContractError(
            "Merge requires current successful remote check evidence."
        )
    names = []
    for entry in checks:
        fields(
            entry,
            {
                "name",
                "producer_id",
                "head_sha",
                "conclusion",
                "url",
                "check_id",
                "workflow_id",
                "run_id",
                "run_attempt",
                "suite_id",
            },
        )
        text(entry["name"], 200)
        text(entry["url"], 500)
        for key in (
            "producer_id",
            "check_id",
            "workflow_id",
            "run_id",
            "run_attempt",
            "suite_id",
        ):
            positive(entry[key])
        if (
            entry["head_sha"] != workspace["commit_sha"]
            or entry["conclusion"] != "success"
        ):
            raise NativeContractError(
                "Merge checks must pass for the current candidate head."
            )
        names.append(entry["name"])
    if len(set(names)) != len(names):
        raise NativeContractError("Merge checks must have unique trusted identities.")
    reviews = value["reviews"]
    if not isinstance(reviews, list) or len(reviews) not in {0, 2}:
        raise NativeContractError(
            "Review evidence must match the optional configured review set."
        )
    for entry in reviews:
        fields(
            entry,
            {
                "role",
                "status_id",
                "creator_id",
                "context",
                "report_url",
                "report_digest",
                "report",
            },
        )
        positive(entry["status_id"])
        positive(entry["creator_id"])
        text(entry["context"], 200)
        text(entry["report_url"], 500)
        sha(entry["report_digest"])
    if reviews and {entry["role"] for entry in reviews} != {"root", "independent"}:
        raise NativeContractError("Merge requires independent and root reviews.")
    if profile is not None:
        profile = merge_profile(profile)
        if (
            policy["profile_digest"] != digest(profile)
            or policy["actor_id"] != profile["actor_id"]
        ):
            raise NativeContractError("Merge receipt differs from trusted profile.")
        expected = {
            (entry["name"], entry["app_id"], entry["workflow_id"])
            for entry in profile["checks"]
        }
        actual = {
            (entry["name"], entry["producer_id"], entry["workflow_id"])
            for entry in checks
        }
        if actual != expected:
            raise NativeContractError(
                "Merge receipt must cover the complete required check set."
            )
        if {entry["role"] for entry in reviews} != {
            entry["role"] for entry in profile["reviews"]
        }:
            raise NativeContractError(
                "Merge receipt must cover the configured optional review set exactly."
            )
        for entry in reviews:
            configured = next(
                value for value in profile["reviews"] if value["role"] == entry["role"]
            )
            if (
                entry["creator_id"] != configured["creator_id"]
                or entry["context"] != configured["context"]
            ):
                raise NativeContractError(
                    "Merge review producer differs from trusted configuration."
                )
    return copied(value)


@checked
def review_report(value, *, role, run_id, context_digest, workspace, pull_number):
    fields(
        value,
        {
            "schema_version",
            "role",
            "run_id",
            "context_digest",
            "repository_id",
            "pull_number",
            "head_sha",
            "base_sha",
            "tree_sha",
            "session_id",
            "reviewer",
            "findings",
            "verdict",
        },
    )
    if type(value["schema_version"]) is not int:
        raise NativeContractError("Review report schema version must be an integer.")
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
        raise NativeContractError(
            "Review report does not authorize this exact candidate."
        )
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
