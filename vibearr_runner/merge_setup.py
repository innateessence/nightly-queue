"""Preview an explicit one-time required-check and no-bypass ruleset update."""

import argparse
from copy import deepcopy
import json
import os
from pathlib import Path

from .merge_contract import merge_profile
from .merge_github import MergeGitHub
from .merge_review import private_document
from .native_contract import fields
from .native_workspace import atomic_bytes
from .recovery import ReconciliationRequired


RULESET_FIELDS = {
    "name",
    "target",
    "enforcement",
    "bypass_actors",
    "conditions",
    "rules",
}


def setup_plan(github, profile, *, base, ruleset_id):
    profile = merge_profile(profile)
    actor = github.request("GET", "/user")
    if actor.get("id") != profile["actor_id"]:
        raise ValueError("Setup credential must identify the selected delivery actor.")
    repository, _ = github.repository(repository_id=profile["repository_id"])
    name = repository["repository"]
    current = github.request(
        "GET", f"/repos/{name}/rulesets/{ruleset_id}?includes_parents=true"
    )
    if (
        current.get("id") != ruleset_id
        or current.get("source_type") != "Repository"
        or current.get("source") != name
    ):
        raise ValueError(
            "Select an owned repository ruleset; inherited rules require their own explicit setup."
        )
    if current.get("target") != "branch" or current.get("enforcement") != "active":
        raise ValueError("Select the existing active branch ruleset.")
    if current.get("conditions", {}).get("ref_name") != {
        "include": ["refs/heads/" + base],
        "exclude": [],
    }:
        raise ValueError(
            "One-time setup requires a ruleset scoped exactly to the configured base."
        )
    original = {key: deepcopy(current[key]) for key in RULESET_FIELDS}
    desired = deepcopy(original)
    desired["bypass_actors"] = []
    required = [
        {"context": value["name"], "integration_id": value["app_id"]}
        for value in profile["checks"]
    ]
    required.extend(
        {"context": value["context"], "integration_id": None}
        for value in profile["reviews"]
    )
    replacement = {
        "type": "required_status_checks",
        "parameters": {
            "strict_required_status_checks_policy": True,
            "do_not_enforce_on_create": True,
            "required_status_checks": required,
        },
    }
    desired["rules"] = [
        entry for entry in desired["rules"] if entry["type"] != "required_status_checks"
    ]
    desired["rules"].append(replacement)
    workspace = {
        "repository_id": repository["repository_id"],
        "repository": name,
        "base_branch": base,
    }
    workspace["base_revision"] = github.base_sha(workspace)
    workspace["commit_sha"] = workspace["base_revision"]
    for configured in profile["checks"]:
        import hashlib

        content = github.content(
            name, configured["workflow_path"], workspace["base_revision"]
        )
        if hashlib.sha256(content).hexdigest() != configured["workflow_digest"]:
            raise ValueError(
                "Trusted workflow digest differs from the current accepted base; review and register it first."
            )
        raw = github.request(
            "GET", f"/repos/{name}/actions/workflows/{configured['workflow_id']}"
        )
        if (
            raw.get("path") != configured["workflow_path"]
            or raw.get("id") != configured["workflow_id"]
            or raw.get("state") != "active"
        ):
            raise ValueError(
                "Trusted workflow provider identity differs from setup configuration."
            )
    return {
        "schema_version": 1,
        "repository_id": repository["repository_id"],
        "repository": name,
        "base": base,
        "base_sha": workspace["base_revision"],
        "control_base": profile["control_base"],
        "control_base_sha": github.base_sha(
            {**workspace, "base_branch": profile["control_base"]}
        ),
        "ruleset_id": ruleset_id,
        "original": original,
        "desired": desired,
    }


def apply_setup(github, profile, *, base, ruleset_id, checkpoint=None, apply=False):
    plan = setup_plan(github, profile, base=base, ruleset_id=ruleset_id)
    if not apply:
        return {"apply": False, "plan": plan}
    if not checkpoint:
        raise ValueError(
            "Applying setup requires a private checkpoint and original-policy backup."
        )
    path = Path(checkpoint)
    if path.exists():
        record = private_document(path)
        for key in ("repository_id", "repository", "base", "ruleset_id", "desired"):
            if record["plan"][key] != plan[key]:
                raise ValueError(
                    "Setup checkpoint belongs to a different policy change."
                )
    else:
        record = {"plan": plan, "dispatched": False}
        descriptor = os.open(
            path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
        )
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(json.dumps(record, sort_keys=True).encode())
            stream.flush()
            os.fsync(stream.fileno())
    expected = record["plan"]["desired"]
    if plan["original"] != expected:
        if record["dispatched"]:
            raise ReconciliationRequired(
                "Ruleset update outcome differs from its original checkpoint; inspect before any retry."
            )
        if plan["original"] != record["plan"]["original"]:
            raise ValueError("Repository rules changed after setup preview.")
        record["dispatched"] = True
        atomic_bytes(path, json.dumps(record, sort_keys=True).encode())
        github.request(
            "PUT", f"/repos/{plan['repository']}/rulesets/{ruleset_id}", expected
        )
        return {"apply": True, "pending_reconciliation": True, "ruleset_id": ruleset_id}
    workspace = {
        "repository_id": plan["repository_id"],
        "repository": plan["repository"],
        "base_branch": base,
        "base_revision": plan["base_sha"],
        "commit_sha": plan["base_sha"],
    }
    evidence = github.policy(workspace, profile)
    return {"apply": True, "qualified": True, "policy": evidence}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--token-file", required=True)
    parser.add_argument("--base", default="main")
    parser.add_argument("--ruleset-id", type=int, required=True)
    parser.add_argument("--checkpoint")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if args.apply and not args.checkpoint:
        parser.error(
            "--apply requires a private --checkpoint containing the original policy"
        )
    token = private_document(args.token_file)
    fields(token, {"secret"})
    github = MergeGitHub(token["secret"], read_only=not args.apply)
    result = apply_setup(
        github,
        private_document(args.profile),
        base=args.base,
        ruleset_id=args.ruleset_id,
        checkpoint=args.checkpoint,
        apply=args.apply,
    )
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
