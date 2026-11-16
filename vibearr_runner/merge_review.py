"""Explicit trusted-coordinator publication of immutable AI review statuses."""

import argparse
import json
import os
from pathlib import Path
import re
import stat

from .merge_contract import merge_profile, review_report
from .merge_github import MergeGitHub, exact_pull
from .native_contract import fields, identifier, sha, validate_workspace
from .native_workspace import atomic_bytes
from .recovery import ReconciliationRequired


def private_document(path):
    target = Path(path)
    descriptor = os.open(target, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as stream:
        details = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(details.st_mode)
            or details.st_uid != os.getuid()
            or stat.S_IMODE(details.st_mode) != 0o600
        ):
            raise ValueError(
                "Trusted coordinator inputs must be owned regular0600 files."
            )
        data = stream.read(262145)
        if len(data) > 262144:
            raise ValueError("Trusted coordinator input exceeds the size limit.")
        return json.loads(data)


def review_status(github, *, profile, binding, role, report_url):
    profile = merge_profile(profile)
    fields(binding, {"run_id", "context_digest", "workspace", "pull_number"})
    identifier(binding["run_id"])
    sha(binding["context_digest"])
    workspace = validate_workspace(binding["workspace"])
    configured = next(entry for entry in profile["reviews"] if entry["role"] == role)
    actor = github.request("GET", "/user")
    if actor.get("id") != configured["creator_id"]:
        raise ValueError(
            "Selected coordinator credential is not this trusted review producer."
        )
    raw = github.pull(workspace["repository"], binding["pull_number"])
    exact_pull(raw, workspace, binding["pull_number"])
    if raw.get("state") != "open" or raw.get("merged"):
        raise ValueError("Review publication requires the exact active PR head.")
    pattern = rf"https://github\.com/{re.escape(configured['report_repository'])}/blob/([0-9a-f]{{40}})/(.+)"
    match = re.fullmatch(pattern, report_url)
    if not match or not match[2].startswith(configured["report_prefix"]):
        raise ValueError(
            "Review report must use the configured immutable control repository URL."
        )
    if (
        configured["report_repository_id"] == workspace["repository_id"]
        and match[1] == workspace["commit_sha"]
    ):
        raise ValueError(
            "Publish AI review reports on a separate control branch without changing the candidate head."
        )
    identity, _ = github.repository(
        repository_url=f"https://github.com/{configured['report_repository']}.git"
    )
    if identity["repository_id"] != configured["report_repository_id"]:
        raise ValueError(
            "Review report repository identity differs from trusted configuration."
        )
    content = github.content(configured["report_repository"], match[2], match[1])
    review_report(
        json.loads(content),
        role=role,
        run_id=binding["run_id"],
        context_digest=binding["context_digest"],
        workspace=workspace,
        pull_number=binding["pull_number"],
    )
    return {
        "state": "success",
        "context": configured["context"],
        "target_url": report_url,
        "description": f"Trusted {role} AI review: no P2 or higher findings",
    }


def publish_review(
    github, *, profile, binding, role, report_url, state_file, apply=False
):
    payload = review_status(
        github, profile=profile, binding=binding, role=role, report_url=report_url
    )
    if not apply:
        return {"apply": False, "status": payload, "binding": binding}
    path = Path(state_file)
    expected = {"binding": binding, "role": role, "payload": payload}
    if path.exists():
        state = private_document(path)
        if any(state.get(key) != value for key, value in expected.items()):
            raise ValueError(
                "Review publication checkpoint belongs to a different candidate."
            )
    else:
        state = {**expected, "dispatched": False}
        descriptor = os.open(
            path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
        )
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(json.dumps(state, sort_keys=True).encode())
            stream.flush()
            os.fsync(stream.fileno())
    workspace = binding["workspace"]
    statuses = github.pages(
        f"/repos/{workspace['repository']}/commits/{workspace['commit_sha']}/statuses"
    )
    matches = [
        value for value in statuses if value.get("context") == payload["context"]
    ]
    if matches:
        observed = max(matches, key=lambda value: value.get("id", 0))
        creator = next(
            value["creator_id"] for value in profile["reviews"] if value["role"] == role
        )
        if (
            all(observed.get(key) == value for key, value in payload.items())
            and observed.get("creator", {}).get("id") == creator
        ):
            return {"apply": True, "status_id": observed["id"], "reconciled": True}
    if state["dispatched"]:
        raise ReconciliationRequired(
            "Trusted review status outcome remains unknown; reconcile this checkpoint before any retry."
        )
    state["dispatched"] = True
    atomic_bytes(path, json.dumps(state, sort_keys=True).encode())
    github.request(
        "POST",
        f"/repos/{workspace['repository']}/statuses/{workspace['commit_sha']}",
        payload,
    )
    return {"apply": True, "pending_reconciliation": True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--binding", required=True)
    parser.add_argument("--role", choices=("independent", "root"), required=True)
    parser.add_argument("--report-url", required=True)
    parser.add_argument("--token-file", required=True)
    parser.add_argument("--state-file")
    parser.add_argument("--publish", action="store_true")
    args = parser.parse_args()
    if args.publish and not args.state_file:
        parser.error("--publish requires a private --state-file checkpoint")
    token = private_document(args.token_file)
    fields(token, {"secret"})
    github = MergeGitHub(token["secret"], read_only=not args.publish)
    result = publish_review(
        github,
        profile=private_document(args.profile),
        binding=private_document(args.binding),
        role=args.role,
        report_url=args.report_url,
        state_file=args.state_file,
        apply=args.publish,
    )
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
