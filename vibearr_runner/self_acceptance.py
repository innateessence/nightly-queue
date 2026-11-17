"""Finite self-development acceptance using the ordinary native controller."""

import argparse
from datetime import datetime, timezone
import math
import json
from pathlib import Path
from uuid import UUID
from urllib.parse import urlencode

from .bootstrap_files import private_json
from .client import APIError
from .delivery import DeliveryError
from .journal import Journal, JournalError
from .native_evidence import validate_native_evidence
from .native_evidence import digest as validate_digest
from .native_github import NativeGitHub, native_pull
from .native_workspace import atomic_bytes
from .queue_acceptance import client, run_acceptance


DELIVERY_TASKS = (
    (
        "instructions-file",
        "Add UTF-8 file/stdin instructions to the standalone CLI",
        "Add `vibearr add TITLE --project ID --instructions-file PATH`. PATH='-' reads stdin. Preserve UTF-8 text and newlines. Make --instructions and --instructions-file mutually exclusive; keep the current default empty instructions when neither is selected. Report unreadable files and invalid UTF-8 clearly before any HTTP request. Keep the CLI standard-library-only and Python3.10 compatible.\n\nAdd focused unittest coverage for a real temporary file, stdin, conflicting flags, missing file, invalid UTF-8, preserved newlines, and the nq compatibility entrypoint. Do not edit workflows, dependencies, validation configuration or queue UI filters. Run the relevant CLI tests. This is a small production-source improvement; deliver it as its own PR.",
    ),
    (
        "version",
        "Show the standalone CLI version before configuration or HTTP",
        "Add `vibearr --version` and `nq --version` using installed vibearr-cli distribution metadata, with a documented deterministic source-checkout fallback. Keep the CLI standard-library-only and Python3.10 compatible; do not import tomllib unconditionally. --version must succeed without API tokens or a valid server URL and must never make an HTTP request. Preserve the existing required subcommand behavior for ordinary invocations.\n\nAdd unittest coverage for distribution metadata, source fallback, both entrypoints, missing token, invalid URL, and no HTTP. Do not edit workflows, dependencies, validation configuration or queue UI filters. Run the relevant CLI tests. This is an independent small production-source improvement; deliver it as its own PR.",
    ),
)
FAILURE_TASK = (
    "version-regression",
    "Add a regression test for the currently unsupported CLI --version flag",
    "Add one focused unittest regression expecting `vibearr --version` to succeed without API configuration and without making HTTP requests. The current CLI does not support --version. This is deliberately a test-only validation-failure acceptance issue: add the regression test but do not implement --version, alter production code, mark the test skipped/expectedFailure, catch the failure as success, edit workflows/dependencies/validation recipes, or weaken any existing test. Keep Python3.10 compatibility.\n\nRun the focused test and report its actual nonzero outcome. The independently configured nine-check validation profile must reject this candidate, and no branch push or PR should be produced. The operator selects repair_limit=0 before claim; this issue cannot change that policy. A later independent source-improvement issue implements --version normally.",
)
MERGE_TASKS = (
    (
        "status-filter",
        "Add a queue status filter",
        "Add a queue status filter with tests for each valid status and malformed input. Preserve existing title/priority/project filters. The trusted operator captures a real Chrome screenshot from the exact candidate in an owned app during CI. Do not download a browser or fabricate a screenshot. Do not implement the follow-up link/clear behavior in this task.",
    ),
    (
        "status-links",
        "Preserve the queue status in filter links and clear behavior",
        "Depends on: #<actual-status-filter-issue>\n\nPreserve the queue status filter in filter links and clear behavior. Add regression tests. The trusted operator captures a real Chrome screenshot from the exact candidate in an owned app during CI; do not download a browser or fabricate a screenshot. The task must start from a default-branch base containing the prerequisite's actual merge commit.",
    ),
)


def scenario_tasks():
    def document(value):
        key, title, body = value
        return {
            "key": key,
            "title": title,
            "body": f"<!-- vibearr-self-acceptance: {key} -->\nBranch: acceptance/{key}\n\n{body}\n",
        }

    return {
        "schema_version": 1,
        "delivery": {
            "order": ["version-regression", "instructions-file", "version"],
            "failure_repair_limit": 0,
            "normal_repair_limit": 2,
            "tasks": [
                document(FAILURE_TASK),
                *[document(value) for value in DELIVERY_TASKS],
            ],
        },
        "merge": {
            "available": True,
            "reason": "Explicit merge execution requires qualified no-bypass policy, all configured CI producers and real UI screenshot receipts.",
            "tasks": [document(value) for value in MERGE_TASKS],
        },
    }


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
    delivery_mode="pull_request",
):
    from .native_contract import validate_native_artifact
    from vibearr_executor.validation_profile import CHECK_IDS

    if delivery_mode not in {"pull_request", "merge"}:
        raise ValueError("Choose pull_request or merge delivery readback.")
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
    expected = (
        "failed" if failed else ("merged" if delivery_mode == "merge" else "delivered")
    )
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
    approvals = queue.request("GET", f"/api/v1/runner/runs/{run_id}/approvals/")[
        "approvals"
    ]
    if approvals:
        raise ValueError(
            "Native acceptance must not create or consume legacy approvals."
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
        "delivery_recovery": run.get("delivery_recovery"),
        "finished_at": run["finished_at"],
        "outcome": expected,
        "approvals": 0,
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
        if not any(value["id"] in {"django", "cli"} for value in nonzero):
            raise ValueError(
                "The deliberate CLI regression must fail an independent application/CLI test check, not stop at an unrelated formatting/tool error."
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
        or delivery["delivery"]["mode"] != delivery_mode
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
    observed, provider_snapshot, current = observe_pull(
        github, delivery, snapshot if delivery_mode == "pull_request" else None
    )
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


def collect_scenario(
    queue,
    github,
    *,
    project_id,
    failure,
    first,
    second,
    review_observer=None,
    snapshots=None,
):
    records = [
        collect_run(
            queue,
            github,
            run_id,
            project_id,
            issue,
            failed=index == 0,
            review_observer=review_observer,
            snapshot=(snapshots or {}).get(run_id),
        )
        for index, (run_id, issue) in enumerate((failure, first, second))
    ]
    if (
        len({value["run_id"] for value in records}) != 3
        or len({value["issue_number"] for value in records}) != 3
    ):
        raise ValueError("Each acceptance case requires a distinct issue and run.")
    deliveries = records[1:]
    if any("vibearr/cli.py" not in value["changed_files"] for value in deliveries):
        raise ValueError(
            "Both approved delivery cases must include a real production CLI source change."
        )
    if (
        len({value["delivery"]["workspace"]["branch"] for value in deliveries}) != 2
        or len(
            {
                value["delivery"]["delivery"]["pull_request"]["number"]
                for value in deliveries
            }
        )
        != 2
    ):
        raise ValueError("Independent source tasks require distinct branches and PRs.")
    if timestamp(records[0]["finished_at"]) >= timestamp(
        records[1]["agent"]["receipt"]["started_at"]
    ):
        raise ValueError(
            "An independent task must demonstrably start after the validation failure."
        )
    first_merge = deliveries[0]["provider_current_observation"]["provider_merge"]
    if first_merge is None:
        raise ValueError(
            "Publish improvementB only after improvementA's actual provider main merge."
        )
    merge_sha = first_merge["commit_sha"]
    next_base = deliveries[1]["delivery"]["workspace"]["base_revision"]
    if next_base != merge_sha:
        repository = deliveries[1]["delivery"]["workspace"]["repository"]
        ancestry = github.request(
            "GET", f"/repos/{repository}/compare/{merge_sha}...{next_base}"
        )
        if (
            ancestry.get("status") != "ahead"
            or ancestry.get("merge_base_commit", {}).get("sha") != merge_sha
        ):
            raise ValueError(
                "ImprovementB did not start from an accepted base containing improvementA's actual merge."
            )
    return {
        "schema_version": 1,
        "scenario": "delivery",
        "passed": True,
        "records": records,
        "first_merge_in_second_base": {
            "merge_sha": merge_sha,
            "base_sha": next_base,
            "ancestor": True,
        },
        "merge_acceptance": "not-run-feature07",
        "api_key_live_acceptance": (
            "passed"
            if records[1]["agent_intent"]["data"]["session"]["auth_kind"] == "token"
            else "not-run-no-dedicated-api-key"
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


def execute(directory, project_id, issues, evidence, max_seconds):
    if len(issues) not in {1, 2, 3} or len(set(issues)) != len(issues):
        raise ValueError(
            "Select a failure issue, then optionally improvementA after restoring repair policy and improvementB after A's reviewed merge."
        )
    with Journal(directory):
        selection_path = directory / "self-acceptance-selection.json"
        selection = (
            private_json(selection_path)
            if selection_path.exists()
            else {"project_id": project_id, "issues": list(issues)}
        )
        if (
            selection["project_id"] != project_id
            or list(issues[: len(selection["issues"])]) != selection["issues"]
        ):
            raise ValueError(
                "Acceptance resume changed an already selected issue/project."
            )
        atomic_bytes(
            selection_path,
            json.dumps({"project_id": project_id, "issues": list(issues)}).encode(),
        )
    runs = []
    receipts = []
    snapshots = {}
    for index, issue in enumerate(issues):
        receipt = directory / f"acceptance-issue-{issue}.json"
        expected = "failed" if index == 0 else "delivered"
        completed = private_json(receipt) if receipt.exists() else None
        if completed and (
            completed["project_id"] != project_id
            or completed["issue_number"] != issue
            or completed["outcome"] != expected
        ):
            raise ValueError(
                "Acceptance receipt belongs to another selected issue/outcome."
            )
        if not completed:
            queue = configured_queue(directory)
            progress_path = directory / f"acceptance-progress-{issue}.json"
            prior = private_json(progress_path) if progress_path.exists() else {}
            verify_phase_policy(
                queue,
                project_id,
                issue,
                failed=index == 0,
                existing_run=prior.get("run_id"),
            )
            if index and receipts[0].get("reporting_acknowledged") is not True:
                raise ValueError(
                    "Confirm the failed run's acknowledgement before claiming the independent source improvement."
                )
        result = (
            {"run_id": completed["run_id"]}
            if completed
            else run_acceptance(
                directory,
                project_id,
                issue,
                receipt,
                max_seconds,
                progress_name=f"acceptance-progress-{issue}.json",
                expected_outcome=expected,
            )
        )
        runs.append((result["run_id"], issue))
        receipts.append(private_json(receipt))
        if receipts[-1].get("reporting_acknowledged") is not True:
            raise ValueError(
                "Acceptance requires actual completion acknowledgement before the next phase."
            )
        queue, github = observers(directory, project_id)
        snapshot_path = directory / f"acceptance-observation-{issue}.json"
        saved_snapshot = private_json(snapshot_path) if snapshot_path.exists() else None
        record = collect_run(
            queue,
            github,
            result["run_id"],
            project_id,
            issue,
            failed=index == 0,
            snapshot=saved_snapshot,
        )
        if saved_snapshot is None:
            atomic_bytes(
                snapshot_path, json.dumps(record, sort_keys=True, indent=2).encode()
            )
            saved_snapshot = record
        snapshots[result["run_id"]] = saved_snapshot
    report = (
        collect_scenario(
            queue,
            github,
            project_id=project_id,
            failure=runs[0],
            first=runs[1],
            second=runs[2],
            snapshots=snapshots,
        )
        if len(runs) == 3
        else {
            "schema_version": 1,
            "scenario": "delivery",
            "passed": False,
            "pending": (
                "Restore the normal repair allowance and preserve task-window counts before publishing improvementA."
                if len(runs) == 1
                else "Publish improvementB only after independent review/CI and root's merge of improvementA."
            ),
            "records": [
                collect_run(
                    queue,
                    github,
                    run_id,
                    project_id,
                    issue,
                    failed=index == 0,
                    snapshot=snapshots[run_id],
                )
                for index, (run_id, issue) in enumerate(runs)
            ],
        }
    )
    report["restarts"] = [
        restart_evidence(record, receipt)
        for record, receipt in zip(report["records"][1:], receipts[1:])
    ]
    atomic_bytes(evidence, json.dumps(report, sort_keys=True, indent=2).encode())
    return {
        "scenario": "delivery",
        "passed": report["passed"],
        "evidence": str(evidence),
        "runs": [value[0] for value in runs],
    }


def observers(directory, project_id):
    queue = configured_queue(directory)
    project = next(
        value
        for value in queue.configuration()["projects"]
        if value["project_id"] == project_id
    )
    grant = queue.request(
        "POST",
        "/api/v1/runner/secrets/",
        {
            "scope": "preflight",
            "backend": "github",
            "credential_id": project["effective"]["repository_credential_id"],
            "project_id": project_id,
        },
    )
    return queue, NativeGitHub(grant["material"]["secret"], read_only=True)


def configured_queue(directory):
    with Journal(directory) as journal:
        state = journal.load()
    queue = client()
    queue.controller_secret = state.get("controller_secret")
    return queue


def verify_phase_policy(queue, project_id, issue_number, *, failed, existing_run=None):
    active = queue.active()
    if active.get("run") is not None:
        item = active.get("item") or {}
        matching = (
            item.get("project", {}).get("id") == project_id
            and item.get("issue_number") == issue_number
        )
        if not matching or existing_run not in {None, active["run"]["id"]}:
            raise ValueError(
                "Reconcile the other active owned run before starting an acceptance phase."
            )
        return
    if existing_run is not None:
        return
    project = next(
        value
        for value in queue.configuration()["projects"]
        if value["project_id"] == project_id
    )
    policy = project["execution_policy"]
    repairs = policy["repair_limit"]
    correct = type(repairs) is int and (repairs == 0 if failed else 0 < repairs <= 2)
    if not correct or policy["delivery_mode"] != "pull_request":
        raise ValueError(
            "Select repair_limit0 for the failure phase or restore normal repair_limit1..2 for source improvements, with PR-only delivery, before claim."
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--export-scenarios", action="store_true")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--scenario", choices=("delivery", "merge"), default="delivery")
    parser.add_argument("--state", type=Path)
    parser.add_argument("--project-id", type=int)
    parser.add_argument("--failure-issue", type=int)
    parser.add_argument("--first-issue", type=int)
    parser.add_argument("--second-issue", type=int)
    parser.add_argument("--evidence", type=Path)
    parser.add_argument("--max-seconds", type=int, default=3600)
    parser.add_argument("--screenshots", type=Path)
    args = parser.parse_args()
    if args.export_scenarios:
        print(json.dumps(scenario_tasks(), indent=2, sort_keys=True))
        return 0
    if not args.execute:
        parser.error("Explicitly choose --execute.")
    if args.scenario == "merge":
        from .merge_acceptance import main as merge_main

        if args.failure_issue is not None:
            parser.error("Merge acceptance selects only its two useful UI issues.")
        forwarded = ["--execute", "--max-seconds", str(args.max_seconds)]
        for field in (
            "state",
            "project_id",
            "first_issue",
            "second_issue",
            "evidence",
            "screenshots",
        ):
            value = getattr(args, field)
            if value is not None:
                forwarded.extend(["--" + field.replace("_", "-"), str(value)])
        return merge_main(forwarded)
    identities = (
        args.project_id,
        args.failure_issue,
    )
    if args.first_issue is not None:
        identities += (args.first_issue,)
    if args.second_issue is not None:
        if args.first_issue is None:
            parser.error("Select improvementA before improvementB.")
        identities += (args.second_issue,)
    if (
        any(type(value) is not int or not 0 < value < 2**63 for value in identities)
        or args.state is None
        or args.evidence is None
        or not 60 <= args.max_seconds <= 7200
    ):
        parser.error(
            "Select positive project/issue IDs, private state, evidence path and bounded60..7200 second duration per case."
        )
    try:
        result = execute(
            args.state, args.project_id, identities[1:], args.evidence, args.max_seconds
        )
    except (
        APIError,
        DeliveryError,
        JournalError,
        OSError,
        ValueError,
        RuntimeError,
        KeyError,
        TypeError,
        AttributeError,
        StopIteration,
    ) as exc:
        print(
            json.dumps(
                {
                    "passed": False,
                    "error": type(exc).__name__,
                    "reason": "Preserve the private journal and reconcile the same operations; no receipt is inferred from missing evidence.",
                }
            )
        )
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
