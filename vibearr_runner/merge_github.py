"""Provider observations for exact-head, non-bypass squash merges."""

import base64
from datetime import datetime
from functools import wraps
import hashlib
import json
import re
from urllib.parse import quote, urlencode

from .delivery import DeliveryError, DeliveryRejected
from .merge_contract import merge_profile, review_report
from .native_contract import digest
from .native_github import NativeGitHub, native_pull


class MergeWait(Exception):
    pass


class MergeRepair(Exception):
    pass


def observed(operation):
    @wraps(operation)
    def call(*args, **kwargs):
        try:
            return operation(*args, **kwargs)
        except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
            raise DeliveryError(
                "Provider merge observation is malformed or incomplete."
            ) from None

    return call


@observed
def exact_pull(raw, workspace, number):
    normalized = dict(raw)
    normalized["state"] = "open"
    pull = native_pull(
        normalized,
        workspace["repository_id"],
        workspace["repository"],
        workspace["branch"],
        workspace["base_branch"],
        workspace["commit_sha"],
    )
    if pull["number"] != number:
        raise DeliveryError("Provider returned a different pull request.")
    return pull


@observed
def merged_observation(raw, workspace, number):
    exact_pull(raw, workspace, number)
    if raw.get("merged") is not True:
        return None
    merge_sha = raw.get("merge_commit_sha")
    if (
        raw.get("state") != "closed"
        or not isinstance(merge_sha, str)
        or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", merge_sha)
    ):
        raise DeliveryError("Merged PR lacks its provider merge commit.")
    try:
        when = datetime.fromisoformat(raw["merged_at"].replace("Z", "+00:00"))
        if when.utcoffset() is None:
            raise ValueError
    except (KeyError, TypeError, ValueError, AttributeError):
        raise DeliveryError("Merged PR lacks a provider merge timestamp.") from None
    return {
        "commit_sha": merge_sha,
        "head_sha": workspace["commit_sha"],
        "merged_at": when.isoformat(),
    }


class MergeGitHub(NativeGitHub):
    @observed
    def pages(self, path, key=None):
        values = []
        for page in range(1, 11):
            separator = "&" if "?" in path else "?"
            raw = self.request("GET", f"{path}{separator}per_page=100&page={page}")
            batch = raw.get(key) if key and isinstance(raw, dict) else raw
            if not isinstance(batch, list):
                raise DeliveryError("Provider list response is invalid.")
            values.extend(batch)
            if len(batch) < 100:
                if key and raw.get("total_count", len(values)) > len(values):
                    raise DeliveryError("Provider list is incomplete.")
                return values
        raise DeliveryError("Provider list exceeds the bounded pagination limit.")

    @observed
    def content(self, repository, path, revision):
        raw = self.request(
            "GET",
            f"/repos/{repository}/contents/{quote(path, safe='/')}?{urlencode({'ref': revision})}",
        )
        try:
            if (
                raw["type"] != "file"
                or raw["encoding"] != "base64"
                or raw["path"] != path
            ):
                raise ValueError
            data = base64.b64decode(raw["content"], validate=False)
            if len(data) > 262144:
                raise ValueError
            return data
        except (KeyError, TypeError, ValueError):
            raise DeliveryError(
                "Trusted provider file could not be read exactly."
            ) from None

    @observed
    def base_sha(self, workspace):
        path = quote(workspace["base_branch"], safe="")
        raw = self.request(
            "GET", f"/repos/{workspace['repository']}/git/ref/heads/{path}"
        )
        if (
            raw.get("ref") != "refs/heads/" + workspace["base_branch"]
            or raw.get("object", {}).get("type") != "commit"
        ):
            raise DeliveryError("Base branch identity is invalid.")
        value = raw["object"].get("sha")
        if not isinstance(value, str) or not re.fullmatch(
            r"[0-9a-f]{40}|[0-9a-f]{64}", value
        ):
            raise DeliveryError("Base branch revision is invalid.")
        return value

    @observed
    def policy(self, workspace, profile):
        profile = merge_profile(profile)
        actor = self.request("GET", "/user")
        if actor.get("id") != profile["actor_id"]:
            raise DeliveryError(
                "Delivery actor differs from trusted merge configuration."
            )
        identity, _ = self.repository(repository_id=profile["repository_id"])
        if (
            identity["repository_id"] != workspace["repository_id"]
            or identity["repository"] != workspace["repository"]
        ):
            raise DeliveryError("Merge repository differs from the claimed repository.")
        repository = workspace["repository"]
        raw = self.request("GET", f"/repos/{repository}")
        if raw.get("allow_squash_merge") is not True:
            raise DeliveryError("Repository must permit squash merge.")
        base = quote(workspace["base_branch"], safe="")
        rules = self.request("GET", f"/repos/{repository}/rules/branches/{base}")
        if not isinstance(rules, list) or not rules:
            raise DeliveryError("Automatic merge requires enforced branch rules.")
        rulesets = {entry.get("ruleset_id") for entry in rules}
        if None in rulesets:
            raise DeliveryError("Effective rule provenance is unavailable.")
        receipts = []
        for identity in sorted(rulesets):
            raw = self.request(
                "GET", f"/repos/{repository}/rulesets/{identity}?includes_parents=true"
            )
            if (
                raw.get("id") != identity
                or raw.get("enforcement") != "active"
                or raw.get("bypass_actors") != []
            ):
                raise DeliveryError(
                    "Every effective ruleset must enforce rules without bypass actors."
                )
            receipts.append({"id": identity, "digest": digest(raw)})
        required = {}
        strict = False
        for entry in rules:
            kind, params = entry.get("type"), entry.get("parameters", {})
            if kind == "required_status_checks":
                strict = (
                    strict or params.get("strict_required_status_checks_policy") is True
                )
                for check in params.get("required_status_checks", []):
                    name = check.get("context")
                    producer = check.get("integration_id")
                    if name in required and required[name] != producer:
                        raise DeliveryError("Required status producer rules conflict.")
                    required[name] = producer
            elif kind == "pull_request":
                if (
                    params.get("required_approving_review_count", 0)
                    or params.get("require_code_owner_review")
                    or params.get("require_last_push_approval")
                    or params.get("required_review_thread_resolution")
                    or params.get("required_reviewers")
                ):
                    raise DeliveryError(
                        "Repository review rules require an unsupported merge gate."
                    )
            elif kind != "required_linear_history":
                raise DeliveryError(f"Unsupported effective merge rule: {kind}.")
        expected = {entry["name"]: entry["app_id"] for entry in profile["checks"]}
        expected.update({entry["context"]: None for entry in profile["reviews"]})
        if not strict or not required or required != expected:
            raise DeliveryError(
                "Enforced required checks must exactly match the nonempty trusted merge profile."
            )
        try:
            self.request("GET", f"/repos/{repository}/branches/{base}/protection")
        except DeliveryRejected as exc:
            if exc.status != 404:
                raise
        else:
            raise DeliveryError(
                "Classic branch protection requires separate qualification; remove overlapping rules or use rulesets."
            )
        for check in profile["checks"]:
            path = check["workflow_path"]
            base_content = self.content(repository, path, workspace["base_revision"])
            candidate = self.content(repository, path, workspace["commit_sha"])
            if (
                hashlib.sha256(base_content).hexdigest() != check["workflow_digest"]
                or candidate != base_content
            ):
                raise DeliveryError(
                    "Candidate or base changed a trusted required workflow."
                )
            raw = self.request(
                "GET", f"/repos/{repository}/actions/workflows/{check['workflow_id']}"
            )
            if (
                raw.get("id") != check["workflow_id"]
                or raw.get("path") != path
                or raw.get("state") != "active"
            ):
                raise DeliveryError("Required workflow identity changed.")
        return {
            "actor_id": actor["id"],
            "profile_digest": digest(profile),
            "rulesets": receipts,
        }

    @observed
    def checks(self, workspace, pull_number, profile):
        repository = workspace["repository"]
        head = workspace["commit_sha"]
        runs = self.pages(
            f"/repos/{repository}/commits/{head}/check-runs?filter=latest", "check_runs"
        )
        observed = []
        for configured in profile["checks"]:
            matches = [
                entry for entry in runs if entry.get("name") == configured["name"]
            ]
            if not matches:
                raise MergeWait("Required remote check is missing.")
            if len(matches) != 1:
                raise DeliveryError(
                    "Required check name has duplicate or forged producers."
                )
            check = matches[0]
            if (
                check.get("head_sha") != head
                or check.get("app", {}).get("id") != configured["app_id"]
            ):
                raise DeliveryError("Remote check has the wrong head or producer.")
            workflow_runs = self.pages(
                f"/repos/{repository}/actions/workflows/{configured['workflow_id']}/runs?{urlencode({'head_sha': head, 'event': 'pull_request'})}",
                "workflow_runs",
            )
            if not workflow_runs:
                raise MergeWait("Trusted workflow has no exact-head run.")
            latest = max(workflow_runs, key=lambda value: value.get("id", 0))
            suite = check.get("check_suite", {}).get("id")
            if latest.get("check_suite_id") != suite:
                raise MergeWait("A newer workflow run supersedes this check.")
            self.workflow_binding(latest, configured, workspace, pull_number)
            jobs = self.pages(
                f"/repos/{repository}/actions/runs/{latest['id']}/attempts/{latest['run_attempt']}/jobs",
                "jobs",
            )
            matches = [
                value for value in jobs if value.get("name") == configured["name"]
            ]
            if (
                len(matches) != 1
                or matches[0].get("id") != check.get("id")
                or matches[0].get("head_sha") != head
            ):
                raise DeliveryError(
                    "Required check does not belong to the current workflow attempt."
                )
            for value in (latest, check, matches[0]):
                if value.get("status") != "completed":
                    raise MergeWait("Required exact-head checks are still pending.")
                if value.get("conclusion") != "success":
                    raise MergeRepair("Required remote validation did not succeed.")
            if not check.get("completed_at") or not check.get("started_at"):
                raise DeliveryError("Required check lacks completed timing evidence.")
            started = datetime.fromisoformat(check["started_at"].replace("Z", "+00:00"))
            completed = datetime.fromisoformat(
                check["completed_at"].replace("Z", "+00:00")
            )
            if (
                started.utcoffset() is None
                or completed.utcoffset() is None
                or completed < started
            ):
                raise DeliveryError("Required check has invalid completion timing.")
            observed.append(
                {
                    "name": configured["name"],
                    "producer_id": configured["app_id"],
                    "head_sha": head,
                    "conclusion": "success",
                    "url": check["html_url"],
                    "check_id": check["id"],
                    "workflow_id": latest["workflow_id"],
                    "run_id": latest["id"],
                    "run_attempt": latest["run_attempt"],
                    "suite_id": suite,
                }
            )
        return observed

    @observed
    def workflow_binding(self, raw, configured, workspace, number):
        if (
            raw.get("workflow_id") != configured["workflow_id"]
            or raw.get("path") != configured["workflow_path"]
            or raw.get("event") != "pull_request"
            or raw.get("head_sha") != workspace["commit_sha"]
            or raw.get("head_repository", {}).get("id") != workspace["repository_id"]
        ):
            raise DeliveryError(
                "Workflow provenance differs from the trusted candidate."
            )
        pulls = raw.get("pull_requests", [])
        matches = [entry for entry in pulls if entry.get("number") == number]
        if len(matches) != 1:
            raise DeliveryError("Workflow run is not associated with the expected PR.")
        pull = matches[0]
        for key, ref, sha in (
            ("base", workspace["base_branch"], workspace["base_revision"]),
            ("head", workspace["branch"], workspace["commit_sha"]),
        ):
            value = pull.get(key, {})
            if (
                value.get("ref") != ref
                or value.get("sha") != sha
                or value.get("repo", {}).get("id") != workspace["repository_id"]
            ):
                raise MergeRepair("Workflow tested a different base/head pair.")

    @observed
    def confirm_checks(self, workspace, profile, receipts):
        repository, head = workspace["repository"], workspace["commit_sha"]
        observed = []
        for receipt in receipts:
            configured = next(
                value for value in profile["checks"] if value["name"] == receipt["name"]
            )
            check = self.request(
                "GET", f"/repos/{repository}/check-runs/{receipt['check_id']}"
            )
            run = self.request(
                "GET",
                f"/repos/{repository}/actions/runs/{receipt['run_id']}/attempts/{receipt['run_attempt']}",
            )
            if (
                check.get("id") != receipt["check_id"]
                or check.get("name") != configured["name"]
                or check.get("head_sha") != head
                or check.get("app", {}).get("id") != configured["app_id"]
                or check.get("check_suite", {}).get("id") != receipt["suite_id"]
            ):
                raise DeliveryError(
                    "Historical check differs from its provider-verified merge receipt."
                )
            if (
                run.get("id") != receipt["run_id"]
                or run.get("run_attempt") != receipt["run_attempt"]
                or run.get("workflow_id") != configured["workflow_id"]
                or run.get("path") != configured["workflow_path"]
                or run.get("head_sha") != head
                or run.get("head_repository", {}).get("id")
                != workspace["repository_id"]
                or run.get("check_suite_id") != receipt["suite_id"]
                or run.get("event") != "pull_request"
            ):
                raise DeliveryError(
                    "Historical workflow provenance differs from the actual merged candidate."
                )
            jobs = self.pages(
                f"/repos/{repository}/actions/runs/{receipt['run_id']}/attempts/{receipt['run_attempt']}/jobs",
                "jobs",
            )
            matches = [
                value
                for value in jobs
                if value.get("id") == receipt["check_id"]
                and value.get("name") == configured["name"]
                and value.get("head_sha") == head
            ]
            if len(matches) != 1 or any(
                value.get("status") != "completed"
                or value.get("conclusion") != "success"
                for value in (check, run, matches[0])
            ):
                raise DeliveryError(
                    "Historical merged candidate checks do not have successful immutable provider results."
                )
            observed.append(receipt)
        return observed

    @observed
    def reviews(self, workspace, pull_number, profile, *, run_id, context_digest):
        if not profile["reviews"]:
            return []
        statuses = self.pages(
            f"/repos/{workspace['repository']}/commits/{workspace['commit_sha']}/statuses"
        )
        receipts = []
        sessions = set()
        for configured in profile["reviews"]:
            matches = [
                value
                for value in statuses
                if value.get("context") == configured["context"]
            ]
            if not matches:
                raise MergeWait("Waiting for trusted independent/root AI review.")
            status = max(matches, key=lambda value: value.get("id", 0))
            if status.get("creator", {}).get("id") != configured["creator_id"]:
                raise DeliveryError("AI review status has an unexpected producer.")
            if status.get("state") == "pending":
                raise MergeWait("Trusted AI review is pending.")
            if status.get("state") != "success":
                raise MergeRepair("Trusted AI review reported unresolved findings.")
            url = status.get("target_url", "")
            pattern = rf"https://github\.com/{re.escape(configured['report_repository'])}/blob/([0-9a-f]{{40}})/(.+)"
            match = re.fullmatch(pattern, url)
            if not match or not match[2].startswith(configured["report_prefix"]):
                raise DeliveryError(
                    "AI review requires an immutable trusted report URL."
                )
            if (
                configured["report_repository_id"] == workspace["repository_id"]
                and match[1] == workspace["commit_sha"]
            ):
                raise DeliveryError(
                    "Candidate commits cannot publish their own trusted review reports."
                )
            identity, _ = self.repository(
                repository_url=f"https://github.com/{configured['report_repository']}.git"
            )
            if identity["repository_id"] != configured["report_repository_id"]:
                raise DeliveryError("AI review report repository identity changed.")
            content = self.content(configured["report_repository"], match[2], match[1])
            report = review_report(
                json.loads(content),
                role=configured["role"],
                run_id=run_id,
                context_digest=context_digest,
                workspace=workspace,
                pull_number=pull_number,
            )
            if report["session_id"] in sessions:
                raise DeliveryError(
                    "Independent and root reviews require different AI sessions."
                )
            sessions.add(report["session_id"])
            receipts.append(
                {
                    "role": configured["role"],
                    "status_id": status["id"],
                    "creator_id": configured["creator_id"],
                    "context": configured["context"],
                    "report_url": url,
                    "report_digest": hashlib.sha256(content).hexdigest(),
                    "report": report,
                }
            )
        return receipts

    def merge_once(self, workspace, number):
        return self.request(
            "PUT",
            f"/repos/{workspace['repository']}/pulls/{number}/merge",
            {"sha": workspace["commit_sha"], "merge_method": "squash"},
        )


class CompletionGitHub(MergeGitHub):
    def __init__(self, *args, repository, issue_number, branch, **kwargs):
        super().__init__(*args, **kwargs)
        self.completion_repository = repository
        self.completion_issue = issue_number
        self.completion_branch = branch

    def request(self, method, path, payload=None):
        if method != "GET":
            issue = (
                f"/repos/{self.completion_repository}/issues/{self.completion_issue}"
            )
            branch = f"/repos/{self.completion_repository}/git/refs/heads/{quote(self.completion_branch, safe='')}"
            permitted = (
                method == "POST"
                and path == issue + "/comments"
                or method == "PATCH"
                and path == issue
                and payload == {"state": "closed", "state_reason": "completed"}
                or method == "DELETE"
                and path == branch
                and payload is None
            )
            if not permitted:
                raise DeliveryError(
                    "Completion credential permits only this merged task's comment, closure and branch cleanup."
                )
        return super().request(method, path, payload)
