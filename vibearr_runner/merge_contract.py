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
    fields(
        value,
        {
            "schema_version",
            "repository_id",
            "actor_id",
            "checks",
            "reviews",
            "ci_timeout_seconds",
            "review_timeout_seconds",
            "control_base",
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
    review_timeout = value["review_timeout_seconds"]
    if type(review_timeout) is not int or not 60 <= review_timeout <= 3600:
        raise NativeContractError(
            "Aggregate AI review allowance must be between60 and3600 seconds."
        )
    from .native_contract import branch

    branch(value["control_base"])
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
    if not isinstance(reviews, list) or len(reviews) not in {2, 3}:
        raise NativeContractError("Configure independent and root AI review producers.")
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
        if (
            entry["report_prefix"].startswith("/")
            or ".." in entry["report_prefix"]
            or not entry["report_prefix"].endswith("/")
        ):
            raise NativeContractError(
                "Review report prefix must be repository relative."
            )
    roles = {entry["role"] for entry in reviews}
    if roles not in (
        {"independent", "root"},
        {"independent", "root", "external_root"},
    ) or len(roles) != len(reviews):
        raise NativeContractError("Both independent and root AI review are required.")
    if len({entry["context"] for entry in reviews}) != len(reviews):
        raise NativeContractError("Review contexts must be distinct.")
    builtin = [entry for entry in reviews if entry["role"] != "external_root"]
    if any(
        entry["report_repository_id"] != value["repository_id"]
        or entry["creator_id"] != value["actor_id"]
        for entry in builtin
    ):
        raise NativeContractError(
            "Built-in reviews publish in the owned repository through the selected actor."
        )
    if (
        len({(entry["report_repository"], entry["report_prefix"]) for entry in builtin})
        != 1
    ):
        raise NativeContractError(
            "Built-in review reports require one fixed control repository and path prefix."
        )
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
    if not isinstance(reviews, list) or len(reviews) not in {2, 3}:
        raise NativeContractError("Merge requires two trusted AI reviews.")
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
    roles = {entry["role"] for entry in reviews}
    if roles not in (
        {"independent", "root"},
        {"independent", "root", "external_root"},
    ) or len(roles) != len(reviews):
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
        if roles != {entry["role"] for entry in profile["reviews"]}:
            raise NativeContractError(
                "Merge receipt must cover every configured AI review gate."
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
def review_document(value, *, role, run_id, context_digest, workspace, pull_number):
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
        if finding["priority"] not in {"P0", "P1", "P2", "P3", "P4"}:
            raise NativeContractError("Review findings require a supported priority.")
        text(finding["description"], 2000)
    blocked = any(entry["priority"] in {"P0", "P1", "P2"} for entry in findings)
    if value["verdict"] != ("changes_requested" if blocked else "no_p2_or_higher"):
        raise NativeContractError("Review verdict differs from its actual findings.")
    return copied(value)


def review_report(value, **binding):
    result = review_document(value, **binding)
    if result["verdict"] != "no_p2_or_higher":
        raise NativeContractError("Review has unresolved P2 or higher findings.")
    return result
