"""Read-only publication evidence for current factory acceptance."""

from datetime import datetime, timezone
import math
from uuid import UUID
from urllib.parse import urlencode
from .native_evidence import validate_native_evidence
from .native_evidence import digest as validate_digest
from .native_github import native_pull


def evidence_entries(queue, run_id):
    UUID(run_id)
    entries = []
    after = 0
    for _ in range(101):
        page = queue.request(
            "GET", f"/api/v1/runner/runs/{run_id}/evidence/", query={"after": after}
        )
        values = page["evidence"]
        if not isinstance(values, list) or len(values) > 10:
            raise ValueError("Acceptance evidence page is invalid.")
        for value in values:
            if type(value.get("id")) is not int or value["id"] <= after:
                raise ValueError("Acceptance evidence cursor did not advance.")
            after = value["id"]
        entries.extend(values)
        if page.get("has_more") is False:
            return entries
        if page.get("next_after") != after:
            raise ValueError("Acceptance evidence cursor differs from the actual page.")
    raise ValueError("Acceptance evidence exceeds its bounded history.")


def timestamp(value):
    if type(value) in {int, float} and math.isfinite(value) and value >= 0:
        return datetime.fromtimestamp(value, timezone.utc)
    if not isinstance(value, str):
        raise ValueError(
            "Acceptance timestamps must be finite epoch seconds or timezone-aware ISO text."
        )
    parsed = datetime.fromisoformat(value)
    if parsed.utcoffset() is None:
        raise ValueError("Acceptance timestamps must be timezone-aware.")
    return parsed


def observe_pull(github, delivery, snapshot=None):
    if snapshot is not None and not isinstance(snapshot, dict):
        raise ValueError("Saved provider observation is not an acceptance record.")
    workspace = delivery["workspace"]
    pull = delivery["delivery"]["pull_request"]
    name = workspace["repository"]
    tested = workspace["commit_sha"]
    raw = github.pull(name, pull["number"])
    current = raw["head"]["sha"]
    validate_digest(current, git=True)
    merged = raw.get("state") == "closed" and raw.get("merged") is True
    if raw.get("draft") is not False:
        raise ValueError("Acceptance requires an observed non-draft pull request.")
    native_pull(
        {**raw, "state": "open"} if merged else raw,
        workspace["repository_id"],
        name,
        workspace["branch"],
        workspace["base_branch"],
        current,
    )
    if snapshot is not None and snapshot.get("delivery") != delivery:
        raise ValueError(
            "Immutable server delivery differs from the saved provider-verified snapshot."
        )
    original = snapshot.get("provider_delivery_snapshot") if snapshot else None
    if original is not None:
        fields = {"observed_at", "head_sha", "base", "number", "repository_id"}
        matches = (
            isinstance(original, dict)
            and set(original) == fields
            and original["head_sha"] == tested
        )
        matches = (
            matches
            and original["base"] == workspace["base_branch"]
            and original["number"] == pull["number"]
            and original["repository_id"] == workspace["repository_id"]
        )
        if not matches or not timestamp(raw["created_at"]) <= timestamp(
            original["observed_at"]
        ) <= datetime.now(timezone.utc):
            raise ValueError(
                "Saved provider observation does not identify the original tested head and observation time."
            )
    changed = current != tested
    if changed:
        if original is None:
            raise ValueError(
                "The provider head changed before an exact-head delivery observation was saved."
            )
        comparison = github.request(
            "GET", f"/repos/{name}/compare/{tested}...{current}"
        )
        if (
            comparison.get("status") != "ahead"
            or comparison.get("merge_base_commit", {}).get("sha") != tested
        ):
            raise ValueError(
                "Post-delivery head does not retain the original tested commit as an ancestor."
            )
    now = datetime.now(timezone.utc).isoformat()
    original = original or {
        "observed_at": now,
        "head_sha": tested,
        "base": workspace["base_branch"],
        "number": pull["number"],
        "repository_id": workspace["repository_id"],
    }
    observed_merge = None
    if merged:
        validate_digest(raw.get("merge_commit_sha"), git=True)
        timestamp(raw.get("merged_at"))
        observed_merge = {
            "commit_sha": raw["merge_commit_sha"],
            "merged_at": raw["merged_at"],
            "base": raw["base"]["ref"],
            "repository_id": raw["base"]["repo"]["id"],
        }
    current_observation = {
        "observed_at": now,
        "head_sha": current,
        "head_changed_after_delivery": changed,
        "original_head_is_ancestor": changed or current == tested,
        "native_validation_applies_to_sha": tested,
        "current_head_native_validated": not changed,
        "provider_merge": observed_merge,
        "review_status": "not-verified-by-this-driver",
    }
    return raw, original, current_observation


def pulls_for_branch(github, repository, branch, base=None):
    query = {
        "state": "all",
        "head": repository.split("/")[0] + ":" + branch,
        "per_page": 100,
    }
    if base is not None:
        query["base"] = base
    return github.request("GET", f"/repos/{repository}/pulls?{urlencode(query)}")


def changed_files(github, workspace):
    name = workspace["repository"]
    base, head = workspace["base_revision"], workspace["commit_sha"]
    comparison = github.request("GET", f"/repos/{name}/compare/{base}...{head}")
    if (
        comparison.get("status") != "ahead"
        or comparison.get("merge_base_commit", {}).get("sha") != base
    ):
        raise ValueError(
            "Delivered source does not extend the recorded inspected base."
        )
    files = comparison.get("files")
    if not isinstance(files, list) or not 1 <= len(files) < 300:
        raise ValueError(
            "Finite acceptance source diff is empty, truncated or unavailable."
        )
    names = [value.get("filename") for value in files]
    if any(not isinstance(value, str) or not value for value in names):
        raise ValueError("Provider source diff filenames are invalid.")
    return sorted(names)


def collect_run(
    queue,
    github,
    run_id,
    project_id,
    issue_number,
    *,
    failed=False,
    review_observer=None,
    snapshot=None,
):
    from .native_contract import validate_native_artifact
    from vibearr_executor.validation_profile import CHECK_IDS

    response = queue.reconcile(run_id)
    run, item = response["run"], response["item"]
    if snapshot is not None and any(
        snapshot.get(key) != value
        for key, value in {
            "run_id": run_id,
            "project_id": project_id,
            "issue_number": issue_number,
        }.items()
    ):
        raise ValueError(
            "Saved provider observation belongs to another selected run/project/issue."
        )
    if (
        item["project"]["id"] != project_id
        or item["issue_number"] != issue_number
        or run["state"] != "completed"
    ):
        raise ValueError(
            "Acceptance result is not a completed owned run for the selected issue/project."
        )
    expected = "failed" if failed else "delivered"
    if run["native_outcome"] != expected or run["result_status"] != (
        "failed" if failed else "succeeded"
    ):
        raise ValueError(
            "Acceptance terminal outcome differs from the required scenario."
        )
    if run["configuration_snapshot"]["execution_kind"] != "native":
        raise ValueError("Acceptance requires the actual native lifecycle.")
    if (
        item.get("source") != "github"
        or run["configuration_snapshot"].get("autonomous") is not True
    ):
        raise ValueError(
            "Acceptance requires an automatically imported issue in an autonomous project."
        )
    source = run.get("source_snapshot", {})
    if (
        source.get("issue_number") != issue_number
        or source.get("repository_id") != item["github_repository_id"]
    ):
        raise ValueError(
            "Frozen source does not identify the actual selected provider issue/repository."
        )
    entries = evidence_entries(queue, run_id)
    facts = {}
    for entry in entries:
        kind = entry["kind"]
        if kind.startswith("native_"):
            validate_native_artifact(kind, entry["data"])
        facts.setdefault(kind, []).append(entry)
    agents = [
        entry["data"]
        for entry in facts.get("native_agent", [])
        if entry["data"]["successful"]
    ]
    if not agents:
        raise ValueError(
            "Acceptance lacks a genuine successful native editing session."
        )
    intents = {
        entry["operation_id"]: entry["data"] for entry in facts.get("native_intent", [])
    }
    attempts = [
        value["attempt"] for value in intents.values() if value["kind"] == "agent"
    ]
    policy = run["configuration_snapshot"]["execution_policy"]
    if not attempts or max(attempts) > policy["repair_limit"]:
        raise ValueError("Acceptance exceeded its frozen repair budget.")
    repository_id = item["github_repository_id"]
    repository, _ = github.repository(repository_id=repository_id)
    name = repository["repository"]
    issue = github.request("GET", f"/repos/{name}/issues/{issue_number}")
    if (
        issue.get("number") != issue_number
        or issue.get("user", {}).get("id") != item["author_id"]
        or "pull_request" in issue
    ):
        raise ValueError("Provider issue identity/author differs from the owned work.")
    validations = [entry["data"] for entry in facts.get("native_validation", [])]
    base = {
        "run_id": run_id,
        "issue_number": issue_number,
        "project_id": project_id,
        "created_at": issue["created_at"],
        "discovered_at": item["created_at"],
        "claimed_at": run["created_at"],
        "finished_at": run["finished_at"],
        "outcome": expected,
        "repair_attempt": max(attempts),
    }
    times = [
        timestamp(value)
        for value in (
            base["created_at"],
            base["discovered_at"],
            base["claimed_at"],
            agents[0]["receipt"]["started_at"],
            base["finished_at"],
        )
    ]
    if times != sorted(times):
        raise ValueError(
            "Provider discovery/claim/session/completion timing is inconsistent."
        )
    if failed:
        nonzero = [
            {"id": check["id"], "receipt": check["receipt"]}
            for value in validations
            for check in value["checks"]
            if type(check["receipt"]["exit_code"]) is int
            and check["receipt"]["exit_code"] != 0
        ]
        if (
            not nonzero
            or run["result_url"]
            or facts.get("verified_delivery")
            or facts.get("native_delivery")
        ):
            raise ValueError(
                "Failure scenario requires a real nonzero validation receipt and no publication evidence."
            )
        if not any(value["id"] in {"django", "runner"} for value in nonzero):
            raise ValueError(
                "The deliberate test regression must fail an independent application or runner test check, not stop at an unrelated formatting/tool error."
            )
        if policy["repair_limit"] != 0 or max(attempts) != 0:
            raise ValueError(
                "The deliberate failure must use the operator's frozen zero-repair policy."
            )
        branch = item["branch_name"]
        pulls = pulls_for_branch(github, name, branch)
        if not isinstance(pulls, list) or pulls:
            raise ValueError(
                "The failed validation branch unexpectedly has a provider PR."
            )
        return {
            **base,
            "nonzero_receipts": nonzero,
            "branch": branch,
            "no_pull_request": True,
        }
    verified = facts.get("verified_delivery", [])
    if len(verified) != 1:
        raise ValueError("Acceptance requires exactly one server-verified delivery.")
    delivery = validate_native_evidence(verified[0]["data"])
    if (
        delivery["run_id"] != run_id
        or delivery["delivery"]["mode"] != "pull_request"
        or delivery["workspace"]["repository_id"] != repository_id
    ):
        raise ValueError(
            "Acceptance requires the selected run/repository and requested delivery policy."
        )
    checks = delivery["validation"]["checks"]
    if {value["id"] for value in checks} != set(CHECK_IDS):
        raise ValueError("Acceptance is missing one or more fixed validation checks.")
    agent = next(
        (
            value
            for value in agents
            if value["session_id"] == delivery["runtime"]["session_id"]
        ),
        None,
    )
    validation = next(
        (
            value
            for value in validations
            if value["state"] == "passed"
            and value["tree_sha"] == delivery["workspace"]["tree_sha"]
            and {entry["receipt"]["operation_id"] for entry in value["checks"]}
            == {entry["operation_id"] for entry in checks}
        ),
        None,
    )
    if agent is None or validation is None:
        raise ValueError(
            "Acceptance lacks the matching native session and independently validated tree."
        )
    pull = delivery["delivery"]["pull_request"]
    workspace = delivery["workspace"]
    observed, provider_snapshot, current = observe_pull(github, delivery, snapshot)
    matches = pulls_for_branch(
        github, name, workspace["branch"], workspace["base_branch"]
    )
    if (
        not isinstance(matches, list)
        or len(matches) != 1
        or matches[0].get("number") != pull["number"]
    ):
        raise ValueError("Acceptance observed duplicate/missing provider PRs.")
    started = agent["receipt"]["started_at"]
    created = timestamp(issue["created_at"])
    if timestamp(started) < created or timestamp(observed["created_at"]) < timestamp(
        started
    ):
        raise ValueError("Acceptance provider/session timing is inconsistent.")
    return {
        **base,
        "agent": agent,
        "validation": validation,
        "agent_intent": intents[agent["intent_id"]],
        "delivery": delivery,
        "pull_created_at": observed["created_at"],
        "provider_delivery_snapshot": provider_snapshot,
        "provider_current_observation": current,
        "changed_files": changed_files(github, workspace),
        "provider_merged_after_delivery": current["provider_merge"] is not None,
        "issue_to_start_seconds": (timestamp(started) - created).total_seconds(),
        "issue_to_pr_seconds": (
            timestamp(observed["created_at"]) - created
        ).total_seconds(),
        "review": (
            review_observer(github, delivery)
            if review_observer
            else {"status": "not-verified-by-this-driver"}
        ),
    }


def restart_evidence(record, receipt):
    observations = receipt.get("executor_observations", {})
    operation = record["agent"]["receipt"]["operation_id"]
    actual = observations.get(operation, [])
    if len(actual) < 2 or len({value["pid"] for value in actual}) < 2:
        raise ValueError(
            "Acceptance lacks an actual agent observation across independent supervisor processes."
        )
    from .native_contract import validate_execution_identity

    identity = validate_execution_identity(
        record["agent"]["receipt"].get("execution_identity")
    )
    if any(
        value["run_id"] != record["run_id"]
        or value["state"] != "running"
        or value.get("execution_identity") != identity
        for value in actual
    ):
        raise ValueError(
            "Restart observation differs from the final protected agent worker identity."
        )
    validation = [
        values
        for values in observations.values()
        if len(values) > 1
        and values[0]["phase"] == "validation"
        and len({value["pid"] for value in values}) > 1
    ]
    checks = {
        value["receipt"]["operation_id"]: validate_execution_identity(
            value["receipt"].get("execution_identity")
        )
        for value in record["validation"]["checks"]
    }
    if not any(
        values[0]["operation_id"] in checks
        and all(
            value.get("execution_identity") == checks[values[0]["operation_id"]]
            and value["run_id"] == record["run_id"]
            for value in values
        )
        for values in validation
    ):
        raise ValueError(
            "Acceptance lacks independent validation on the same worker boot across a supervisor restart."
        )
    if receipt.get("reporting_acknowledged") is not True:
        raise ValueError("Acceptance lacks the actual completion acknowledgement.")
    return {"checkpoints": receipt["restarts"], "executor_observations": observations}
