"""Provider observations for published pull requests and human merges."""

import base64
from datetime import datetime
from functools import wraps
import re
from urllib.parse import quote, urlencode

from .delivery import DeliveryError
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
            if any(
                value.get("head_sha") != head
                or value.get("app", {}).get("id") != configured["app_id"]
                for value in matches
            ):
                raise DeliveryError("Remote check has the wrong head or producer.")
            workflow_runs = self.pages(
                f"/repos/{repository}/actions/workflows/{configured['workflow_id']}/runs?{urlencode({'head_sha': head, 'event': 'pull_request'})}",
                "workflow_runs",
            )
            if not workflow_runs:
                raise MergeWait("Trusted workflow has no exact-head run.")
            latest = max(workflow_runs, key=lambda value: value.get("id", 0))
            suite = latest.get("check_suite_id")
            self.workflow_binding(latest, configured, workspace, pull_number)
            current = [
                value
                for value in matches
                if value.get("check_suite", {}).get("id") == suite
            ]
            if not current:
                raise MergeWait("A newer workflow run supersedes this check.")
            if len(current) != 1:
                raise DeliveryError(
                    "Required current-suite check has duplicate or forged producers."
                )
            check = current[0]
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
