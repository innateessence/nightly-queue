"""Bounded factory role documents; these do not grant execution authority."""

from copy import deepcopy
import json
import math
import re
from uuid import UUID

from . import native_contract as native

MAX_ARTIFACT_BYTES = 256 * 1024
MAX_DEPTH = 20
MAX_ROUND = 100
REVIEWERS = {"reviewer-code", "reviewer-security", "reviewer-ux", "reviewer-ui"}
AGENT_ROLES = {"intake", "specification", "build", "decision"} | REVIEWERS
ROLES = AGENT_ROLES | {"checks", "routing"}
FIELDS = {
    "schema_version",
    "run_id",
    "operation_id",
    "role",
    "round",
    "context_id",
    "context_digest",
    "specification_id",
    "specification_digest",
    "source_digest",
    "tree_sha",
    "base_sha",
    "head_sha",
    "configuration_digest",
    "payload",
}
EXECUTION_FIELDS = {
    "intent_id",
    "result_id",
    "adapter",
    "model",
    "agent_version",
    "session_id",
    "operation_id",
    "execution_identity",
    "input_source_digest",
    "input_digest",
    "prompt_digest",
    "started_at",
    "ended_at",
}


class FactoryContractError(ValueError):
    pass


def identifier(value):
    if not isinstance(value, str):
        raise FactoryContractError("Factory identifiers must be UUID strings.")
    try:
        if str(UUID(value)) != value:
            raise ValueError
    except (ValueError, AttributeError):
        raise FactoryContractError("Use canonical factory UUID identifiers.") from None
    return value


def sha(value, *, git=False, empty=False):
    if empty and value == "":
        return value
    pattern = r"[a-f0-9]{40}|[a-f0-9]{64}" if git else r"[a-f0-9]{64}"
    if not isinstance(value, str) or not re.fullmatch(pattern, value):
        raise FactoryContractError("Factory source and document digests are invalid.")
    return value


def text(value, *, empty=False, limit=10000):
    if not isinstance(value, str) or len(value) > limit or "\x00" in value:
        raise FactoryContractError("Factory text must be bounded Unicode strings.")
    if not empty and not value.strip():
        raise FactoryContractError("Factory text cannot be empty.")
    return value


def bounded(value, depth=0):
    if depth > MAX_DEPTH:
        raise FactoryContractError("Reduce factory JSON nesting.")
    if value is None or type(value) is bool or type(value) is int:
        return
    if type(value) is float and math.isfinite(value):
        return
    if isinstance(value, str):
        text(value, empty=True, limit=MAX_ARTIFACT_BYTES)
        return
    if isinstance(value, list):
        if len(value) > 1000:
            raise FactoryContractError("Reduce factory JSON array size.")
        for item in value:
            bounded(item, depth + 1)
        return
    if isinstance(value, dict):
        if len(value) > 1000:
            raise FactoryContractError("Reduce factory JSON object size.")
        for key, item in value.items():
            text(key, limit=200)
            bounded(item, depth + 1)
        return
    raise FactoryContractError("Factory artifacts require finite JSON values.")


def text_list(value, *, empty=True, limit=100):
    if not isinstance(value, list) or len(value) > limit or not empty and not value:
        raise FactoryContractError("Factory fields require bounded lists.")
    for item in value:
        text(item)
    return value


def criteria(value):
    if not isinstance(value, list) or not value or len(value) > 100:
        raise FactoryContractError("Record bounded, testable acceptance criteria.")
    identities = []
    for index, item in enumerate(value):
        if isinstance(item, str):
            text(item)
            identities.append(f"criterion-{index + 1}")
            continue
        if not isinstance(item, dict):
            raise FactoryContractError(
                "Acceptance criteria require text or identified objects."
            )
        identities.append(text(item.get("id"), limit=100))
        text(item.get("description", item.get("text")))
    if len(set(identities)) != len(identities):
        raise FactoryContractError("Acceptance criterion IDs must be unique.")
    return identities


def execution(value):
    if not isinstance(value, dict) or set(value) != EXECUTION_FIELDS:
        raise FactoryContractError(
            "Record the complete native role execution identity."
        )
    for key in ("intent_id", "result_id", "operation_id"):
        identifier(value[key])
    if value["adapter"] not in {"codex", "claude", "opencode"}:
        raise FactoryContractError("Choose a registered native role backend.")
    for key in ("model", "agent_version", "session_id"):
        text(value[key], limit=200)
    for key in ("input_source_digest", "input_digest", "prompt_digest"):
        sha(value[key])
    try:
        native.validate_execution_identity(value["execution_identity"])
    except native.NativeContractError as exc:
        raise FactoryContractError(str(exc)) from None
    for key in ("started_at", "ended_at"):
        number = value[key]
        if type(number) not in {int, float} or not math.isfinite(number) or number < 0:
            raise FactoryContractError(
                "Record actual finite native execution timestamps."
            )
    if value["ended_at"] < value["started_at"]:
        raise FactoryContractError("Native role ended before it started.")


def validate_role_intent(value):
    if not isinstance(value, dict) or set(value) != {
        "role",
        "round",
        "prompt_digest",
        "input_digest",
    }:
        raise FactoryContractError("Record the exact factory role dispatch descriptor.")
    if value["role"] not in AGENT_ROLES:
        raise FactoryContractError("Only native agent roles have dispatch descriptors.")
    if type(value["round"]) is not int or not 1 <= value["round"] <= MAX_ROUND:
        raise FactoryContractError(
            "Factory dispatch requires a bounded positive round."
        )
    sha(value["prompt_digest"])
    sha(value["input_digest"])
    return deepcopy(value)


def validate_payload(role, payload):
    if not isinstance(payload, dict):
        raise FactoryContractError("Factory role payload must be an object.")
    if role == "intake":
        if payload.get("verdict") not in {"accept", "needs_info", "reject"}:
            raise FactoryContractError("Unknown intake verdict.")
        for field in ("scope", "rationale"):
            text(payload.get(field))
        if payload["verdict"] == "accept":
            criteria(payload.get("criteria"))
        text_list(payload.get("questions"))
        if payload["verdict"] == "needs_info" and not payload["questions"]:
            raise FactoryContractError(
                "Needs-information intake requires concrete questions."
            )
    elif role == "specification":
        criteria(payload.get("criteria"))
        text(payload.get("scope"))
        text_list(payload.get("exclusions"))
        text_list(payload.get("verification"), empty=False)
    elif role == "build":
        text(payload.get("summary"))
        if (
            not isinstance(payload.get("criteria"), (list, dict))
            or not payload["criteria"]
        ):
            raise FactoryContractError("Map build changes to the acceptance criteria.")
    elif role == "routing":
        required = text_list(payload.get("required_reviewers"), empty=False, limit=4)
        if len(set(required)) != len(required) or not set(required) <= REVIEWERS:
            raise FactoryContractError("Unknown or duplicate factory reviewer.")
        if "reviewer-code" not in required:
            raise FactoryContractError("Code review is always required.")
        sha(payload.get("rule_digest"))
        for field in ("paths_digest", "settings_digest"):
            if field in payload:
                sha(payload[field])
    elif role == "checks":
        identifier(payload.get("validation_id"))
        if payload.get("state") not in {"passed", "failed"}:
            raise FactoryContractError("Record the actual validation state.")
    elif role in REVIEWERS:
        if payload.get("verdict") not in {"pass", "changes", "blocked"}:
            raise FactoryContractError("Unknown reviewer verdict.")
        findings = payload.get("findings")
        if not isinstance(findings, list) or len(findings) > 100:
            raise FactoryContractError("Record bounded reviewer findings.")
        identities = []
        for finding in findings:
            if not isinstance(finding, dict):
                raise FactoryContractError("Findings must be identified objects.")
            identities.append(text(finding.get("id"), limit=100))
            text(finding.get("summary"))
            text_list(finding.get("evidence"), empty=False)
            text_list(finding.get("criterion_ids"))
        if len(set(identities)) != len(identities):
            raise FactoryContractError("Finding IDs must be unique.")
        text_list(payload.get("coverage"))
    elif role == "decision":
        if payload.get("verdict") not in {"ready", "unresolved"}:
            raise FactoryContractError("Unknown readiness decision.")
        text(payload.get("rationale"))
        for identity in text_list(payload.get("artifact_ids"), empty=False):
            identifier(identity)
    if role in {"intake", "routing"}:
        if payload.get("risk") not in {"low", "medium", "high"}:
            raise FactoryContractError("Record a known risk level.")
        surfaces = text_list(payload.get("surfaces"), limit=2)
        if not set(surfaces) <= {"ui", "security"} or len(set(surfaces)) != len(
            surfaces
        ):
            raise FactoryContractError("Unknown or duplicate affected surface.")


def validate_artifact(value):
    try:
        bounded(value)
        encoded = json.dumps(
            value, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
        if len(encoded) > MAX_ARTIFACT_BYTES:
            raise FactoryContractError("Factory artifact exceeds its byte limit.")
        if not isinstance(value, dict) or value.get("role") not in ROLES:
            raise FactoryContractError("Record a registered factory role.")
        role = value["role"]
        expected = FIELDS | ({"execution"} if role in AGENT_ROLES else set())
        if (
            set(value) != expected
            or type(value["schema_version"]) is not int
            or value["schema_version"] != 1
        ):
            raise FactoryContractError(
                "Record the exact version-1 factory artifact envelope."
            )
        for key in ("run_id", "operation_id", "context_id"):
            identifier(value[key])
        if type(value["round"]) is not int or not 1 <= value["round"] <= MAX_ROUND:
            raise FactoryContractError(
                "Factory round must be a bounded positive integer."
            )
        for key in ("context_digest", "source_digest", "configuration_digest"):
            sha(value[key])
        for key in ("tree_sha", "base_sha"):
            sha(value[key], git=True)
        sha(value["head_sha"], git=True, empty=True)
        if value["specification_id"] is None:
            if value["specification_digest"] != "" or role not in {
                "intake",
                "specification",
            }:
                raise FactoryContractError(
                    "Only intake/specification may lack a prior specification."
                )
        else:
            identifier(value["specification_id"])
            sha(value["specification_digest"])
        if role in AGENT_ROLES:
            execution(value["execution"])
        validate_payload(role, value["payload"])
    except (UnicodeError, RecursionError, ValueError, TypeError, KeyError) as exc:
        if isinstance(exc, FactoryContractError):
            raise
        raise FactoryContractError("Invalid finite factory artifact.") from None
    return deepcopy(value)


def artifact_document(**values):
    return validate_artifact({"schema_version": 1, **values})
