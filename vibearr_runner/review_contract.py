"""Native review ownership and control-report publication contracts."""

import math

from . import native_contract as contract
from .merge_contract import review_document
from .native_sessions import NativeSession


BUILTIN_ROLES = ("independent", "root")
REVIEW_FACTS = {
    "native_review",
    "native_review_publication",
    "native_review_publication_terminal",
}


def validate_review_intent(value):
    data = value["data"]
    if value["kind"] == "review":
        contract.fields(
            data,
            {
                "workspace_id",
                "workspace",
                "pull_request",
                "role",
                "session",
                "diff_digest",
            },
        )
        contract.identifier(data["workspace_id"])
        contract.validate_workspace(data["workspace"])
        contract.pull(data["pull_request"], data["workspace"])
        if data["role"] not in BUILTIN_ROLES:
            raise contract.NativeContractError(
                "Native review roles are independent and root."
            )
        session = NativeSession.from_data(data["session"])
        if session.purpose != "review":
            raise contract.NativeContractError(
                "Review intent requires the fixed instruction-isolated review purpose."
            )
        contract.account(session.account)
        contract.manifest(session.manifest)
        if session.manifest["phase"] != "agent":
            raise contract.NativeContractError(
                "Native review requires the registered native agent launch recipe."
            )
        contract.sha(data["diff_digest"])
        return
    contract.fields(
        data,
        {
            "publication_id",
            "workspace_id",
            "workspace",
            "pull_request",
            "review_ids",
            "repository_id",
            "repository",
            "control_base",
            "base_revision",
            "branch",
            "commit_sha",
            "files",
            "report_paths",
            "stage",
            "role",
        },
    )
    for key in ("publication_id", "workspace_id"):
        contract.identifier(data[key])
    contract.validate_workspace(data["workspace"])
    contract.pull(data["pull_request"], data["workspace"])
    contract.unique_ids(data["review_ids"])
    if len(data["review_ids"]) != 2:
        raise contract.NativeContractError(
            "Control publication requires both owned native review facts."
        )
    contract.positive(data["repository_id"])
    contract.repository(data["repository"])
    for key in ("control_base", "branch"):
        contract.branch(data[key])
    if (
        data["control_base"] in {data["branch"], data["workspace"]["base_branch"]}
        or data["branch"] == data["workspace"]["branch"]
    ):
        raise contract.NativeContractError(
            "Control reports must remain separate from the app base and task branch."
        )
    for key in ("base_revision", "commit_sha"):
        contract.sha(data[key], git=True)
    if data["stage"] not in {"push", "create_pr", "status"}:
        raise contract.NativeContractError("Unknown native review publication stage.")
    if (data["stage"] == "status" and data["role"] not in BUILTIN_ROLES) or (
        data["stage"] != "status" and data["role"] is not None
    ):
        raise contract.NativeContractError(
            "Review status publication requires its exact built-in role."
        )
    contract.fields(data["report_paths"], set(BUILTIN_ROLES))
    files = data["files"]
    if not isinstance(files, list) or not 4 <= len(files) <= 16:
        raise contract.NativeContractError(
            "Control publication requires bounded report and receipt files."
        )
    paths = []
    for entry in files:
        contract.fields(entry, {"path", "digest"})
        path = entry["path"]
        contract.text(path, 500)
        if path.startswith("/") or ".." in path or not path.endswith((".json", ".png")):
            raise contract.NativeContractError(
                "Control changes are JSON and PNG data under a trusted prefix."
            )
        contract.sha(entry["digest"])
        paths.append(path)
    if len(set(paths)) != len(paths) or not set(data["report_paths"].values()) <= set(
        paths
    ):
        raise contract.NativeContractError(
            "Control report paths must identify unique committed files."
        )


def validate_review_artifact(kind, value):
    if kind == "native_review":
        contract.fields(
            value,
            {
                "schema_version",
                "intent_id",
                "role",
                "receipt",
                "successful",
                "session_id",
                "agent_version",
                "usage",
                "report",
                "report_digest",
            },
        )
        contract.identifier(value["intent_id"])
        if value["role"] not in BUILTIN_ROLES:
            raise contract.NativeContractError(
                "Native review result has an invalid role."
            )
        contract.validate_receipt(value["receipt"])
        if type(value["successful"]) is not bool:
            raise contract.NativeContractError("Native review success must be boolean.")
        contract.text(value["session_id"], 200, empty=not value["successful"])
        contract.text(value["agent_version"], 200)
        if not isinstance(value["usage"], list) or len(value["usage"]) > 64:
            raise contract.NativeContractError("Native review usage must be bounded.")
        for entry in value["usage"]:
            contract.fields(entry, {"name", "value", "unit"})
            contract.text(entry["name"], 100)
            contract.text(entry["unit"], 100)
            if (
                type(entry["value"]) not in {int, float}
                or not math.isfinite(entry["value"])
                or entry["value"] < 0
            ):
                raise contract.NativeContractError(
                    "Review usage requires actual finite numeric values."
                )
        contract.sha(value["report_digest"], empty=True)
        if value["successful"]:
            if (
                not contract.receipt_passed(value["receipt"])
                or value["receipt"]["source_digest"]
                != value["receipt"]["export_digest"]
            ):
                raise contract.NativeContractError(
                    "Passing reviews require confirmed unchanged source/export and descendants."
                )
            if (
                not isinstance(value["report"], dict)
                or contract.digest(value["report"]) != value["report_digest"]
            ):
                raise contract.NativeContractError(
                    "Review report digest differs from its protected parsed output."
                )
        elif value["report"] is not None or value["report_digest"]:
            raise contract.NativeContractError(
                "Invalid or incomplete review cannot claim a passing report."
            )
    elif kind == "native_review_publication":
        contract.fields(
            value,
            {
                "schema_version",
                "intent_id",
                "publication_id",
                "stage",
                "pushed_sha",
                "pull_request",
                "status",
            },
        )
        for key in ("intent_id", "publication_id"):
            contract.identifier(value[key])
        contract.sha(value["pushed_sha"], git=True)
        if value["stage"] not in {"push", "create_pr", "status"}:
            raise contract.NativeContractError(
                "Review publication fact has an invalid stage."
            )
        if value["pull_request"] is not None:
            contract.pull(value["pull_request"])
        if value["stage"] == "create_pr" and value["pull_request"] is None:
            raise contract.NativeContractError(
                "Review PR fact requires its observed PR identity."
            )
        status = value["status"]
        if value["stage"] == "status":
            contract.fields(
                status, {"id", "role", "context", "creator_id", "target_url", "state"}
            )
            contract.positive(status["id"])
            contract.positive(status["creator_id"])
            for key in ("context", "target_url"):
                contract.text(status[key], 500)
            if status["role"] not in BUILTIN_ROLES or status["state"] != "success":
                raise contract.NativeContractError(
                    "Review status must represent a successful owned built-in review."
                )
        elif status is not None:
            raise contract.NativeContractError(
                "Only a status fact may claim a provider review status."
            )
    elif kind == "native_review_publication_terminal":
        contract.fields(value, {"schema_version", "intent_id", "state"})
        contract.identifier(value["intent_id"])
        if value["state"] != "not_started":
            raise contract.NativeContractError(
                "Dispatched review publication requires factual reconciliation."
            )
    else:
        raise contract.NativeContractError("Unknown native review artifact.")


def bound_report(value, intent, *, run_id, context_digest):
    data = intent["data"]
    if not value["successful"]:
        return None
    report = review_document(
        value["report"],
        role=data["role"],
        run_id=run_id,
        context_digest=context_digest,
        workspace=data["workspace"],
        pull_number=data["pull_request"]["number"],
    )
    if report["session_id"] != value["session_id"]:
        raise contract.NativeContractError(
            "Review report must identify the actual native session."
        )
    return report
