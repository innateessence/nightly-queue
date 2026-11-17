"""Actual merge-loop evidence; missing provider or runtime proof fails closed."""

import argparse
import json
from pathlib import Path
import re

from .merge_contract import merge_profile, merge_receipt
from .merge_github import MergeGitHub, merged_observation
from .native_workspace import atomic_bytes
from .browser_receipts import validate_receipt as validate_screenshot


MERGE_WAYPOINTS = {
    "merge-dispatched",
    "merge-observed",
    "completion-comment",
    "completion-close",
}


def merge_waypoint(native, ordinary=""):
    data = native.get("merge", {})
    if not data or data.get("completed"):
        return ordinary
    if data.get("close_dispatched"):
        return "completion-close"
    if data.get("comment_dispatched"):
        return "completion-comment"
    if data.get("merge"):
        return "merge-observed"
    if data.get("dispatched"):
        return "merge-dispatched"
    return ordinary


def capture_page(
    *,
    source_checkout,
    revision,
    url,
    output,
    browser_profile,
    browser=None,
):
    raise RuntimeError(
        "Host browser capture is retired; use the Docker browser job and source-bound receipts."
    )


def screenshot_receipts(
    values,
    expected_revision,
    *,
    alternate_revision=None,
    expected_tree=None,
    artifact_root=Path("/screenshots"),
):
    if not isinstance(values, list) or not values:
        raise ValueError("UI acceptance requires actual rendered browser screenshots.")
    for value in values:
        validate_screenshot(
            value,
            expected_revision,
            alternate_revision=alternate_revision,
            expected_tree=expected_tree,
            artifact_root=artifact_root,
        )
    return values


def collect_merge_run(queue, github, *, run_id, project_id, issue_number, screenshots):
    from .self_acceptance import collect_run, evidence_entries

    record = collect_run(
        queue, github, run_id, project_id, issue_number, delivery_mode="merge"
    )
    run = queue.reconcile(run_id)["run"]
    delivery = record["delivery"]
    workspace = delivery["workspace"]
    profile = merge_profile(run["configuration_snapshot"]["effective"]["merge_profile"])
    entries = evidence_entries(queue, run_id)
    merges = [entry for entry in entries if entry["kind"] == "native_merge"]
    completions = [entry for entry in entries if entry["kind"] == "native_completion"]
    if len(merges) != 1 or len(completions) != 1:
        raise ValueError(
            "Merge acceptance requires exactly one verified merge and source completion."
        )
    merged, completion = merges[0]["data"], completions[0]["data"]
    if (
        merged["workspace"] != workspace
        or merged["pull_request"] != delivery["delivery"]["pull_request"]
    ):
        raise ValueError(
            "Provider merge evidence differs from the delivered native candidate."
        )
    merge_receipt(merged["receipt"], profile, workspace)
    provider = (
        github
        if isinstance(github, MergeGitHub)
        else MergeGitHub(github.token, read_only=True)
    )
    pull = delivery["delivery"]["pull_request"]
    observed = merged_observation(
        provider.pull(workspace["repository"], pull["number"]),
        workspace,
        pull["number"],
    )
    if observed != merged["merge"]:
        raise ValueError("Provider does not confirm the recorded exact-head merge.")
    actual_receipt = {
        "policy": provider.policy(workspace, profile),
        "checks": provider.confirm_checks(
            workspace, profile, merged["receipt"]["checks"]
        ),
        "reviews": provider.reviews(
            workspace,
            pull["number"],
            profile,
            run_id=run_id,
            context_digest=delivery["context"]["digest"],
        ),
    }
    if actual_receipt != merged["receipt"]:
        raise ValueError(
            "Actual check/review provenance differs from the merge authorization."
        )
    source = run["source_snapshot"]
    issue = provider.request(
        "GET", f"/repos/{workspace['repository']}/issues/{issue_number}"
    )
    if (
        issue.get("id") != source.get("metadata", {}).get("issue_id")
        or issue.get("state") != "closed"
    ):
        raise ValueError("Provider did not complete the original frozen source issue.")
    from .merge_contract import completion_marker

    recovery = run.get("delivery_recovery")
    if recovery:
        prior = recovery.get("archive", {})
        if (
            recovery.get("generation") != 1
            or prior.get("result_status") != "failed"
            or prior.get("native_outcome") != "failed"
            or not prior.get("reporting_acknowledged_at")
            or not prior.get("finish_digest")
            or prior["finish_digest"] == run["finish_digest"]
        ):
            raise ValueError(
                "Delivery recovery lacks its immutable prior failure and positive acknowledgement."
            )
    marker = completion_marker(run_id, recovery["generation"] if recovery else None)
    comments = provider.pages(
        f"/repos/{workspace['repository']}/issues/{issue_number}/comments"
    )
    matches = [value for value in comments if marker in value.get("body", "")]
    if (
        len(matches) != 1
        or matches[0].get("id") != completion["comment_id"]
        or matches[0].get("user", {}).get("id") != profile["actor_id"]
    ):
        raise ValueError(
            "Completion marker is missing, duplicated or from the wrong producer."
        )
    if (
        completion["merge_intent_id"] != merged["intent_id"]
        or completion["issue_number"] != issue_number
    ):
        raise ValueError(
            "Completion references another source issue or merge operation."
        )
    commit = provider.request(
        "GET", f"/repos/{workspace['repository']}/git/commits/{observed['commit_sha']}"
    )
    if commit.get("tree", {}).get("sha") != workspace["tree_sha"]:
        raise ValueError(
            "Actual squash merge tree differs from the independently validated candidate."
        )
    screenshot_receipts(
        screenshots,
        observed["commit_sha"],
        alternate_revision=workspace["commit_sha"],
        expected_tree=workspace["tree_sha"],
    )
    return {
        **record,
        "merge": merged,
        "merge_recorded_at": merges[0]["created_at"],
        "completion": completion,
        "screenshots": screenshots,
    }


def collect_merge_scenario(
    queue, github, *, project_id, first, second, screenshots, restarts, receipts=None
):
    from .self_acceptance import restart_evidence, timestamp

    records = [
        collect_merge_run(
            queue,
            github,
            run_id=run_id,
            project_id=project_id,
            issue_number=issue,
            screenshots=screenshots[run_id],
        )
        for run_id, issue in (first, second)
    ]
    if first[0] == second[0] or first[1] == second[1]:
        raise ValueError("Dependent UI tasks require distinct real issues and runs.")
    if (
        len({value["delivery"]["workspace"]["branch"] for value in records}) != 2
        or len(
            {
                value["delivery"]["delivery"]["pull_request"]["number"]
                for value in records
            }
        )
        != 2
    ):
        raise ValueError("Dependent UI tasks require distinct branches and PRs.")
    for value in records:
        changed = value["changed_files"]
        if not any(
            path.startswith("work_queue/")
            and path.endswith((".py", ".html", ".js"))
            and not re.search(r"(^|/)(test[^/]*|tests)/?", path)
            for path in changed
        ):
            raise ValueError(
                "Both MVP tasks require a useful queue UI source change, not documentation-only changes."
            )
        if not any(
            path.startswith("work_queue/")
            and Path(path).name.startswith("test")
            and path.endswith(".py")
            for path in changed
        ):
            raise ValueError(
                "Both MVP UI changes require their actual regression test changes."
            )
    a, b = records
    merge_sha = a["merge"]["merge"]["commit_sha"]
    response = queue.reconcile(second[0])
    source = response["run"]["source_snapshot"]
    directive = re.search(
        r"(?im)^Depends on:\s*(#[1-9][0-9]*(?:,\s*#[1-9][0-9]*)*)\s*$",
        source.get("body", ""),
    )
    dependencies = (
        [int(value) for value in re.findall(r"#([1-9][0-9]*)", directive[1])]
        if directive
        else []
    )
    if first[1] not in dependencies:
        raise ValueError(
            "The second frozen source must explicitly depend on the first source issue."
        )
    contexts = response["run"].get("context_reference")
    context = queue.request(
        "GET", f"/api/v1/runner/runs/{second[0]}/contexts/{contexts}/"
    )
    document = context.get("document", context.get("context", {}).get("document", {}))
    initial_base = document.get("repository", {}).get("base_revision")
    if not initial_base:
        raise ValueError("The second task lacks its immutable initial base context.")
    name = b["delivery"]["workspace"]["repository"]
    ancestry = github.request(
        "GET", f"/repos/{name}/compare/{merge_sha}...{initial_base}"
    )
    if initial_base != merge_sha and (
        ancestry.get("status") != "ahead"
        or ancestry.get("merge_base_commit", {}).get("sha") != merge_sha
    ):
        raise ValueError(
            "Second task initial base does not contain the first provider merge."
        )
    if timestamp(b["claimed_at"]) < timestamp(a["merge_recorded_at"]):
        raise ValueError(
            "Dependent task was claimed before trusted merge evidence existed."
        )
    for run_id, _ in (first, second):
        observed = restarts.get(run_id, {})
        if not MERGE_WAYPOINTS <= observed.keys():
            raise ValueError("Actual merge/completion restart evidence is incomplete.")
        selected = [observed[key] for key in MERGE_WAYPOINTS]
        if len({value["pid"] for value in selected}) != len(selected):
            raise ValueError(
                "Merge/completion checkpoints were not reconciled by distinct supervisor processes."
            )
    if not receipts or any(value["run_id"] not in receipts for value in records):
        raise ValueError(
            "Actual agent/validation restart and completion acknowledgement receipts are required."
        )
    runtime_restarts = [
        restart_evidence(value, receipts[value["run_id"]]) for value in records
    ]
    if any(
        record.get("delivery_recovery")
        != receipts[record["run_id"]].get("delivery_recovery")
        for record in records
    ):
        raise ValueError(
            "Delivery recovery audit differs from the captured completion receipt."
        )
    return {
        "schema_version": 1,
        "scenario": "merge",
        "passed": True,
        "records": records,
        "restarts": restarts,
        "runtime_restarts": runtime_restarts,
        "first_merge_in_second_initial_base": {
            "merge_sha": merge_sha,
            "initial_base_sha": initial_base,
            "ancestor": True,
        },
        "release_scope": "Two dependent source merges; other MVP rows require their separate real acceptance reports.",
        "mvp_ready": False,
    }


def write_report(path, value):
    atomic_bytes(Path(path), json.dumps(value, sort_keys=True, indent=2).encode())


def execute(
    directory,
    *,
    project_id,
    first_issue,
    second_issue,
    evidence,
    max_seconds,
    screenshots=None,
):
    from .bootstrap_files import private_json
    from .journal import Journal
    from .queue_acceptance import run_acceptance
    from .self_acceptance import observers

    directory = Path(directory)
    selected = {"project_id": project_id, "issues": [first_issue, second_issue]}
    with Journal(directory):
        path = directory / "merge-acceptance-selection.json"
        if path.exists() and private_json(path) != selected:
            raise ValueError(
                "Merge acceptance selection changed while its runs may still own resources."
            )
        write_report(path, selected)
    runs, receipts, restarts = [], [], {}
    for issue in selected["issues"]:
        path = directory / f"merge-acceptance-issue-{issue}.json"
        saved = private_json(path) if path.exists() else None
        if saved and (
            saved.get("project_id") != project_id
            or saved.get("issue_number") != issue
            or saved.get("outcome") != "merged"
        ):
            raise ValueError(
                "Saved merge receipt belongs to another selected task/outcome."
            )
        result = (
            {"run_id": saved["run_id"]}
            if saved
            else run_acceptance(
                directory,
                project_id,
                issue,
                path,
                max_seconds,
                progress_name=f"acceptance-progress-{issue}.json",
                expected_outcome="merged",
            )
        )
        receipt = private_json(path)
        runs.append((result["run_id"], issue))
        receipts.append(receipt)
        restarts[result["run_id"]] = receipt["restarts"]
    if screenshots is None:
        report = {
            "schema_version": 1,
            "scenario": "merge",
            "passed": False,
            "mvp_ready": False,
            "pending": "Collect actual rendered UI screenshots for both provider merge revisions, then resume with their private receipt JSON.",
            "runs": runs,
            "receipts": receipts,
        }
    else:
        queue, github = observers(directory, project_id)
        report = collect_merge_scenario(
            queue,
            github,
            project_id=project_id,
            first=runs[0],
            second=runs[1],
            screenshots=screenshots,
            restarts=restarts,
            receipts={value["run_id"]: value for value in receipts},
        )
    write_report(evidence, report)
    return {
        "scenario": "merge",
        "passed": report["passed"],
        "mvp_ready": False,
        "evidence": str(evidence),
        "runs": [value[0] for value in runs],
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--export-scenarios", action="store_true")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--state", type=Path)
    parser.add_argument("--project-id", type=int)
    parser.add_argument("--first-issue", type=int)
    parser.add_argument("--second-issue", type=int)
    parser.add_argument("--evidence", type=Path)
    parser.add_argument("--screenshots", type=Path)
    parser.add_argument("--max-seconds", type=int, default=3600)
    args = parser.parse_args(argv)
    if args.export_scenarios:
        from .self_acceptance import scenario_tasks

        document = scenario_tasks()["merge"]
        document.update(
            available=True,
            reason="Explicit real execution requires finite source issues, qualified no-bypass policy and all required CI producers.",
        )
        print(json.dumps(document, sort_keys=True, indent=2))
        return 0
    identities = (args.project_id, args.first_issue, args.second_issue)
    if (
        not args.execute
        or any(type(value) is not int or not 0 < value < 2**63 for value in identities)
        or args.first_issue == args.second_issue
        or args.state is None
        or args.evidence is None
        or not 60 <= args.max_seconds <= 7200
    ):
        parser.error(
            "Explicitly select --execute, one project, two distinct finite issues, owned state/evidence and60..7200 seconds per case."
        )
    from .bootstrap_files import private_json

    screenshots = private_json(args.screenshots) if args.screenshots else None
    result = execute(
        args.state,
        project_id=args.project_id,
        first_issue=args.first_issue,
        second_issue=args.second_issue,
        evidence=args.evidence,
        max_seconds=args.max_seconds,
        screenshots=screenshots,
    )
    print(json.dumps(result, sort_keys=True))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
