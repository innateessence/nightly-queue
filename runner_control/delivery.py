"""Validate attestations from a separately authenticated, trusted supervisor."""

import re
from uuid import UUID

from django.core.exceptions import ValidationError

from coding_runner.approvals import ActionSpec
from coding_runner.context import repository_path, digest
from coding_runner.delivery import DeliveryError, pull_identity

from .plan_services import valid_handoff
from .models import ActionApproval


def reject():
    raise ValidationError(
        {
            "status": "Success requires current plan-bound validation and verified repository, commit, branch, and pull request delivery."
        }
    )


def sha(value, length=None):
    pattern = r"[0-9a-f]{64}" if length == 64 else r"[0-9a-f]{40}|[0-9a-f]{64}"
    if not isinstance(value, str) or not re.fullmatch(pattern, value):
        reject()


def validate_delivery(run, payload):
    try:
        return checked_delivery(run, payload)
    except (KeyError, TypeError, ValueError, AttributeError, DeliveryError):
        reject()


def checked_delivery(run, payload):
    fields = {
        "schema_version",
        "implementation",
        "repository",
        "branch",
        "base_branch",
        "base_revision",
        "commit_sha",
        "tree_sha",
        "pull_request",
        "pushed_revision",
    }
    if (
        not isinstance(payload, dict)
        or set(payload) != fields
        or type(payload["schema_version"]) is not int
        or payload["schema_version"] != 1
    ):
        reject()
    plan, context = valid_handoff(run, require_refresh=False)
    handoff = run.evidence.filter(kind="plan_handoff").last()
    if handoff is None or handoff.payload != {
        "plan_id": str(plan.pk),
        "context_id": str(context.pk),
        "base_commit": context.inspected_base_commit,
    }:
        reject()
    repository = repository_path(run.item.repository_url).lstrip("/")
    if payload["repository"] != repository or payload["branch"] != run.item.branch_name:
        reject()
    if (
        payload["base_branch"] != context.resolved_base_branch
        or payload["base_revision"] != context.inspected_base_commit
    ):
        reject()
    for key in ("base_revision", "commit_sha", "tree_sha", "pushed_revision"):
        sha(payload[key])
    if (
        payload["commit_sha"] != payload["pushed_revision"]
        or payload["commit_sha"] == payload["base_revision"]
    ):
        reject()
    pull = payload["pull_request"]
    raw = {
        "number": pull["number"],
        "html_url": pull["url"],
        "state": "open",
        "head": {
            "repo": {"full_name": pull["repository"]},
            "ref": pull["head"],
            "sha": pull["commit_sha"],
        },
        "base": {"repo": {"full_name": pull["repository"]}, "ref": pull["base"]},
    }
    if (
        pull_identity(
            raw,
            repository,
            payload["branch"],
            payload["base_branch"],
            payload["commit_sha"],
        )
        != pull
    ):
        reject()
    evidence = payload["implementation"]
    expected = {
        "schema_version",
        "plan_id",
        "plan_digest",
        "context_id",
        "workspace",
        "validation",
        "amendments",
    }
    if (
        set(evidence) != expected
        or type(evidence["schema_version"]) is not int
        or evidence["schema_version"] != 1
    ):
        reject()
    if (
        evidence["plan_id"] != str(plan.pk)
        or evidence["plan_digest"] != plan.digest
        or evidence["context_id"] != str(context.pk)
    ):
        reject()
    if not isinstance(evidence["amendments"], list):
        reject()
    workspace = evidence["workspace"]
    keys = {
        "workspace_id",
        "base_revision",
        "branch",
        "head_revision",
        "content_digest",
        "diff_digest",
        "tree_digest",
        "changed",
    }
    if (
        set(workspace) != keys
        or workspace["workspace_id"] != str(run.pk)
        or workspace["changed"] != "yes"
    ):
        reject()
    if (
        workspace["branch"] != payload["branch"]
        or workspace["base_revision"] != payload["base_revision"]
    ):
        reject()
    sha(workspace["head_revision"])
    sha(workspace["content_digest"], 64)
    sha(workspace["diff_digest"], 64)
    sha(workspace["tree_digest"], 64)
    checks = evidence["validation"]
    if not isinstance(checks, list) or not 1 <= len(checks) <= 128:
        reject()
    actions = []
    executions = set()
    for check in checks:
        fields = {
            "phase",
            "action",
            "execution_id",
            "exit_code",
            "interrupted",
            "before",
            "after",
            "output_digest",
            "output_bytes",
        }
        if (
            set(check) != fields
            or check["phase"] != "validation"
            or type(check["exit_code"]) is not int
            or check["exit_code"] != 0
            or check["interrupted"] is not False
        ):
            reject()
        if check["before"] != workspace or check["after"] != workspace:
            reject()
        sha(check["output_digest"], 64)
        execution = str(UUID(check["execution_id"]))
        if (
            execution in executions
            or type(check["output_bytes"]) is not int
            or check["output_bytes"] < 0
        ):
            reject()
        executions.add(execution)
        action = ActionSpec.from_data(check["action"])
        if (
            action.workspace_id != str(run.pk)
            or action.base_revision != workspace["base_revision"]
            or action.content_digest != workspace["content_digest"]
        ):
            reject()
        if not run.approvals.filter(
            execution_id=execution,
            decision=ActionApproval.Decision.APPROVED,
            consumed_at__isnull=False,
            action=action.to_data(),
            binding_digest=action.binding_digest,
        ).exists():
            reject()
        actions.append(action.to_data())
    for action in plan.document["validation_actions"]:
        keys = ("argv", "mode", "source_kind", "source_reference", "source_digest")
        if not any(
            all(record[key] == action[key] for key in keys) for record in actions
        ):
            reject()
    configuration = run.configuration_snapshot.get("effective", {})
    for command in configuration.get("validation_commands", []):
        if not any(
            record["argv"] == ["/bin/sh", "-c", command]
            and record["mode"] == "shell"
            and record["source_kind"] == "operator"
            and record["source_reference"] == "run-configuration:" + str(run.pk)
            and record["source_digest"] == digest(run.configuration_snapshot)
            for record in actions
        ):
            reject()
    return payload


def validate_success(run, result_url):
    evidence = run.evidence.filter(kind="verified_delivery").last()
    if evidence is None:
        reject()
    delivery = validate_delivery(run, evidence.payload)
    if result_url != delivery["pull_request"]["url"]:
        reject()
