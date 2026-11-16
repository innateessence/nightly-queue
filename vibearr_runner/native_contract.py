"""Shared native metadata contracts; no execution or permission decisions."""

from datetime import datetime, timezone
from functools import wraps
import hashlib
import json
import math
import re
from uuid import UUID


class NativeContractError(ValueError):
    pass


def checked(operation):
    @wraps(operation)
    def call(*args, **kwargs):
        try:
            return operation(*args, **kwargs)
        except NativeContractError:
            raise
        except ModuleNotFoundError as exc:
            if exc.name and exc.name.startswith("vibearr_executor"):
                raise NativeContractError(
                    "Install vibearr-runner[native-runtime] to use executor-backed native metadata."
                ) from None
            raise
        except (
            ValueError,
            TypeError,
            KeyError,
            AttributeError,
            OverflowError,
            OSError,
            RecursionError,
        ):
            raise NativeContractError(
                "Native metadata violates its versioned contract."
            ) from None

    return call


ARTIFACT_KINDS = {
    "native_intent",
    "native_agent",
    "native_validation",
    "native_workspace",
    "native_delivery",
    "native_termination",
    "native_revision",
    "native_merge",
    "native_completion",
    "native_merge_terminal",
    "native_review",
    "native_review_publication",
    "native_review_publication_terminal",
}
FACT_KINDS = ARTIFACT_KINDS - {"native_intent"}
WORKSPACE_FIELDS = {
    "repository_id",
    "repository",
    "branch",
    "base_branch",
    "base_revision",
    "tree_sha",
    "commit_sha",
}
PROFILE_FIELDS = {
    "schema_version",
    "profile_id",
    "version",
    "image",
    "database_image",
    "dependency_digest",
    "recipe_digest",
    "required_checks",
    "profile_digest",
}
MANIFEST_FIELDS = {
    "schema_version",
    "run_id",
    "operation_id",
    "profile_id",
    "profile_digest",
    "source_digest",
    "context_digest",
    "phase",
    "limits",
    "deadline",
    "network_hosts",
}


def canonical(value):
    try:
        return json.dumps(
            value, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise NativeContractError(
            "Native metadata must be finite Unicode JSON."
        ) from None


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def copied(value, limit=65536):
    encoded = canonical(value)
    if len(encoded) > limit:
        raise NativeContractError("Native metadata exceeds its byte limit.")
    return json.loads(encoded)


def fields(value, expected):
    if not isinstance(value, dict) or set(value) != set(expected):
        raise NativeContractError("Native metadata has missing or unknown fields.")


def text(value, limit=2000, *, empty=False):
    if not isinstance(value, str) or len(value) > limit or "\x00" in value:
        raise NativeContractError("Native metadata requires bounded Unicode text.")
    if not empty and not value.strip():
        raise NativeContractError("Native metadata requires nonempty text.")
    try:
        value.encode()
    except UnicodeError:
        raise NativeContractError("Native metadata requires Unicode text.") from None
    return value


def identifier(value):
    try:
        if not isinstance(value, str) or str(UUID(value)) != value:
            raise ValueError
    except (ValueError, AttributeError):
        raise NativeContractError(
            "Native control identities must be canonical UUIDs."
        ) from None
    return value


def sha(value, *, git=False, image=False, empty=False):
    if empty and value == "":
        return value
    pattern = r"[a-f0-9]{40}|[a-f0-9]{64}" if git else r"[a-f0-9]{64}"
    if image:
        pattern = r"sha256:[a-f0-9]{64}"
    if not isinstance(value, str) or re.fullmatch(pattern, value) is None:
        raise NativeContractError("Native metadata has an invalid content identity.")
    return value


def positive(value):
    if type(value) is not int or not 0 < value < 2**63:
        raise NativeContractError(
            "Native provider identities must be positive integers."
        )
    return value


def timestamp(value):
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.utcoffset() is None:
            raise ValueError
    except (ValueError, TypeError):
        raise NativeContractError(
            "Native timestamps must include a timezone."
        ) from None
    return parsed


def branch(value):
    text(value, 200)
    if (
        value.startswith(("-", "/"))
        or value.endswith(("/", "."))
        or value in {"@", "HEAD"}
    ):
        raise NativeContractError("Native work requires a valid named branch.")
    if any(part.startswith(".") or part.endswith(".lock") for part in value.split("/")):
        raise NativeContractError("Native branch has a reserved component.")
    if any(part in value for part in ("..", "@{", "//")) or re.search(
        r"[\x00-\x20\x7f~^:?*\[\\]", value
    ):
        raise NativeContractError("Native branch has invalid characters.")
    return value


def repository(value):
    text(value, 201)
    if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value) is None:
        raise NativeContractError("Native delivery requires a GitHub repository name.")
    if any(part in {".", ".."} for part in value.split("/")):
        raise NativeContractError("Native repository name is invalid.")
    return value


def criteria(value):
    if not isinstance(value, list) or len(value) > 64:
        raise NativeContractError("Use at most64 acceptance criteria.")
    for entry in value:
        text(entry, 4000)
    if sum(len(entry.encode()) for entry in value) > 65536:
        raise NativeContractError("Acceptance criteria exceed64 KiB.")
    return value


def account(value):
    fields(value, {"backend", "credential_id", "credential_version", "model"})
    text(value["backend"], 100)
    text(value["model"], 200)
    positive(value["credential_id"])
    positive(value["credential_version"])


@checked
def profile_reference(profile):
    data = profile.data()
    value = {key: data[key] for key in PROFILE_FIELDS - {"profile_digest"}}
    value["profile_digest"] = profile.digest
    profile_from_reference(value)
    return copied(value)


@checked
def profile_from_reference(value):
    from vibearr_executor.validation_profile import ValidationProfile

    fields(value, PROFILE_FIELDS)
    try:
        profile = ValidationProfile(
            image=value["image"],
            database_image=value["database_image"],
            dependency_digest=value["dependency_digest"],
            profile_id=value["profile_id"],
            version=value["version"],
        )
        data = profile.data()
        expected = {key: data[key] for key in PROFILE_FIELDS - {"profile_digest"}}
        expected["profile_digest"] = profile.digest
        if canonical(expected) != canonical(value):
            raise ValueError
    except (ValueError, KeyError, TypeError):
        raise NativeContractError(
            "Validation reference differs from the installed trusted recipe."
        ) from None
    return profile


@checked
def validate_context(value):
    expected = {
        "schema_version",
        "execution_kind",
        "run_id",
        "source",
        "repository",
        "source_digest",
        "policy_digest",
        "account",
        "validation_profile",
        "runtime_binding_digest",
        "fetched_at",
        "acceptance_criteria",
    }
    fields(value, expected)
    if (
        type(value["schema_version"]) is not int
        or value["schema_version"] != 2
        or value["execution_kind"] != "native"
    ):
        raise NativeContractError("Supply a version2 native context.")
    identifier(value["run_id"])
    if not isinstance(value["source"], dict):
        raise NativeContractError("Native context requires a frozen source snapshot.")
    selected = value["repository"]
    fields(
        selected,
        {
            "repository_id",
            "repository",
            "repository_url",
            "base_branch",
            "base_revision",
            "base_tree_sha",
            "branch",
        },
    )
    positive(selected["repository_id"])
    repository(selected["repository"])
    if selected["repository_url"] != f"https://github.com/{selected['repository']}.git":
        raise NativeContractError(
            "Native repository URL differs from its canonical name."
        )
    branch(selected["base_branch"])
    branch(selected["branch"])
    if selected["branch"] == selected["base_branch"]:
        raise NativeContractError("Native work cannot update its base directly.")
    sha(selected["base_revision"], git=True)
    sha(selected["base_tree_sha"], git=True)
    for key in ("source_digest", "policy_digest", "runtime_binding_digest"):
        sha(value[key])
    account(value["account"])
    profile_from_reference(value["validation_profile"])
    timestamp(value["fetched_at"])
    criteria(value["acceptance_criteria"])
    return copied(value, 4 * 1024 * 1024)


@checked
def context_document(
    *,
    run_id,
    source,
    repository,
    source_digest,
    policy_digest,
    account,
    validation_profile,
    runtime_binding_digest,
    acceptance_criteria,
    fetched_at=None,
):
    return validate_context(
        {
            "schema_version": 2,
            "execution_kind": "native",
            "run_id": run_id,
            "source": source,
            "repository": repository,
            "source_digest": source_digest,
            "policy_digest": policy_digest,
            "account": account,
            "validation_profile": (
                profile_reference(validation_profile)
                if hasattr(validation_profile, "data")
                else validation_profile
            ),
            "runtime_binding_digest": runtime_binding_digest,
            "fetched_at": fetched_at or datetime.now(timezone.utc).isoformat(),
            "acceptance_criteria": acceptance_criteria,
        }
    )


@checked
def validate_plan(value):
    fields(
        value,
        {
            "schema_version",
            "execution_kind",
            "context_id",
            "context_digest",
            "problem",
            "approach",
            "acceptance_criteria",
        },
    )
    if (
        type(value["schema_version"]) is not int
        or value["schema_version"] != 2
        or value["execution_kind"] != "native"
    ):
        raise NativeContractError("Supply a version2 native plan.")
    identifier(value["context_id"])
    sha(value["context_digest"])
    text(value["problem"], 2000)
    text(value["approach"], 20000)
    criteria(value["acceptance_criteria"])
    return copied(value, 256 * 1024)


@checked
def plan_document(
    *, context_id, context_digest, problem, approach, acceptance_criteria
):
    return validate_plan(
        {
            "schema_version": 2,
            "execution_kind": "native",
            "context_id": context_id,
            "context_digest": context_digest,
            "problem": problem,
            "approach": approach,
            "acceptance_criteria": acceptance_criteria,
        }
    )


@checked
def validate_workspace(value):
    fields(value, WORKSPACE_FIELDS)
    positive(value["repository_id"])
    repository(value["repository"])
    branch(value["branch"])
    branch(value["base_branch"])
    if value["branch"] == value["base_branch"]:
        raise NativeContractError("Native work cannot publish to its base.")
    for key in ("base_revision", "tree_sha", "commit_sha"):
        sha(value[key], git=True)
    return value


def workspace_identity(value):
    validate_workspace(value)
    return {key: value[key] for key in WORKSPACE_FIELDS - {"repository"}}


@checked
def manifest(value):
    from vibearr_executor.contracts import Limits

    fields(value, MANIFEST_FIELDS)
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise NativeContractError("Unsupported native manifest.")
    for key in ("run_id", "operation_id"):
        identifier(value[key])
    for key in ("profile_digest", "source_digest", "context_digest"):
        sha(value[key])
    text(value["profile_id"], 100)
    if value["phase"] not in {"agent", "validation", "setup"}:
        raise NativeContractError("Unknown native execution phase.")
    Limits.parse(value["limits"])
    timestamp(value["deadline"])
    hosts = value["network_hosts"]
    if not isinstance(hosts, list) or len(hosts) > 64 or len(set(hosts)) != len(hosts):
        raise NativeContractError("Native network hosts must be a bounded unique list.")
    for host in hosts:
        text(host, 253)
    return value


@checked
def intent_document(kind, *, context_id, context_digest, policy_digest, attempt, data):
    return validate_intent(
        {
            "schema_version": 1,
            "kind": kind,
            "context_id": context_id,
            "context_digest": context_digest,
            "policy_digest": policy_digest,
            "attempt": attempt,
            "data": data,
        }
    )


@checked
def agent_intent(
    *, context_id, context_digest, policy_digest, attempt, session, workspace_id=None
):
    data = {
        "session": session.to_data() if hasattr(session, "to_data") else session,
        "workspace_id": workspace_id,
    }
    return intent_document(
        "agent",
        context_id=context_id,
        context_digest=context_digest,
        policy_digest=policy_digest,
        attempt=attempt,
        data=data,
    )


@checked
def validation_intent(
    *,
    context_id,
    context_digest,
    policy_digest,
    attempt,
    session,
    workspace_id,
    provenance=None,
):
    data = copied(session)
    data["profile"] = profile_reference(profile_from_data(data["profile"]))
    data["workspace_id"] = workspace_id
    data["provenance"] = provenance
    return intent_document(
        "validation",
        context_id=context_id,
        context_digest=context_digest,
        policy_digest=policy_digest,
        attempt=attempt,
        data=data,
    )


def profile_from_data(value):
    from vibearr_executor.validation_profile import ValidationProfile

    return ValidationProfile.parse(value)


@checked
def dependency_intent(
    *, context_id, context_digest, policy_digest, attempt, request, workspace_id
):
    data = copied(request)
    data["validation_profile"] = profile_reference(
        profile_from_data(data["validation_profile"])
    )
    data["workspace_id"] = workspace_id
    return intent_document(
        "dependency",
        context_id=context_id,
        context_digest=context_digest,
        policy_digest=policy_digest,
        attempt=attempt,
        data=data,
    )


@checked
def delivery_intent(
    *,
    context_id,
    context_digest,
    policy_digest,
    attempt,
    publication_id,
    stage,
    workspace_id,
    workspace,
):
    data = {
        "publication_id": publication_id,
        "stage": stage,
        "workspace_id": workspace_id,
        "workspace": workspace,
    }
    return intent_document(
        "delivery",
        context_id=context_id,
        context_digest=context_digest,
        policy_digest=policy_digest,
        attempt=attempt,
        data=data,
    )


@checked
def validate_intent(value):
    fields(
        value,
        {
            "schema_version",
            "kind",
            "context_id",
            "context_digest",
            "policy_digest",
            "attempt",
            "data",
        },
    )
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise NativeContractError("Unsupported native intent.")
    identifier(value["context_id"])
    sha(value["context_digest"])
    sha(value["policy_digest"])
    if type(value["attempt"]) is not int or not 0 <= value["attempt"] <= 2:
        raise NativeContractError("Native work permits at most2 repairs.")
    data = value["data"]
    kind = value["kind"]
    if kind == "agent":
        from .native_sessions import NativeSession

        fields(data, {"session", "workspace_id"})
        session = NativeSession.from_data(data["session"])
        if session.purpose != "coding":
            raise NativeContractError(
                "Coding agent intent requires the original coding purpose."
            )
        account(session.account)
        if session.account["backend"] != session.adapter:
            raise NativeContractError(
                "Native session backend differs from its account."
            )
        manifest(session.manifest)
        if session.manifest["phase"] != "agent":
            raise NativeContractError("Native agent intent requires the agent phase.")
        if data["workspace_id"] is not None:
            identifier(data["workspace_id"])
    elif kind == "validation":
        validate_validation_session(data)
    elif kind == "dependency":
        validate_dependency_request(data)
    elif kind == "delivery":
        fields(data, {"publication_id", "stage", "workspace_id", "workspace"})
        identifier(data["publication_id"])
        identifier(data["workspace_id"])
        if data["stage"] not in {"push", "create_pr"}:
            raise NativeContractError("Unknown native publication stage.")
        validate_workspace(data["workspace"])
    elif kind == "revision":
        fields(
            data,
            {
                "publication_id",
                "workspace_id",
                "workspace",
                "pull_request",
                "base_revision",
                "reason",
            },
        )
        identifier(data["publication_id"])
        identifier(data["workspace_id"])
        validate_workspace(data["workspace"])
        pull(data["pull_request"], data["workspace"])
        sha(data["base_revision"], git=True)
        if data["reason"] not in {"base_update", "ci_repair"}:
            raise NativeContractError("Unknown publication revision reason.")
    elif kind == "merge":
        from .merge_contract import merge_profile, merge_receipt

        fields(
            data,
            {
                "publication_id",
                "workspace_id",
                "workspace",
                "pull_request",
                "stage",
                "profile",
                "receipt",
            },
        )
        identifier(data["publication_id"])
        identifier(data["workspace_id"])
        validate_workspace(data["workspace"])
        pull(data["pull_request"], data["workspace"])
        if data["stage"] not in {"merge", "comment", "close", "delete"}:
            raise NativeContractError("Unknown automatic merge mutation stage.")
        merge_profile(data["profile"])
        merge_receipt(data["receipt"], data["profile"], data["workspace"])
    elif kind in {"review", "review_publication"}:
        from .review_contract import validate_review_intent

        validate_review_intent(value)
    else:
        raise NativeContractError("Unknown native intent kind.")
    return copied(value)


def validate_validation_session(data):
    fields(
        data,
        {
            "schema_version",
            "run_id",
            "attempt_id",
            "profile",
            "profile_digest",
            "tree_sha",
            "source_digest",
            "operations",
            "workspace_id",
            "provenance",
        },
    )
    identifier(data["workspace_id"])
    complete = {
        key: entry
        for key, entry in data.items()
        if key not in {"workspace_id", "provenance"}
    }
    complete["profile"] = profile_from_reference(data["profile"]).data()
    from .native_validation import ValidationRunner

    entries = ValidationRunner.checked(complete)
    deadlines = set()
    for entry in entries:
        manifest(entry["manifest"])
        deadlines.add(entry["manifest"]["deadline"])
    if len(deadlines) != 1:
        raise NativeContractError("Validation commands must share the frozen deadline.")
    if data["provenance"] is not None:
        from vibearr_executor.validation_profile import validate_dependency_provenance

        validate_dependency_provenance(
            data["provenance"],
            profile_from_reference(data["profile"]),
            run_id=data["run_id"],
        )


def validate_dependency_request(data):
    from vibearr_executor.contracts import Limits
    from vibearr_executor.validation_profile import PACKAGE_HOSTS

    fields(
        data,
        {
            "schema_version",
            "run_id",
            "build_id",
            "source_digest",
            "setup_profile_id",
            "validation_profile",
            "limits",
            "deadline",
            "network_hosts",
            "workspace_id",
        },
    )
    if type(data["schema_version"]) is not int or data["schema_version"] != 1:
        raise NativeContractError("Unsupported dependency request.")
    for key in ("run_id", "build_id", "workspace_id"):
        identifier(data[key])
    sha(data["source_digest"])
    selected = profile_from_reference(data["validation_profile"])
    if data["setup_profile_id"] != selected.setup_profile().profile_id or data[
        "network_hosts"
    ] != list(PACKAGE_HOSTS):
        raise NativeContractError(
            "Dependency build differs from the fixed setup recipe."
        )
    Limits.parse(data["limits"])
    timestamp(data["deadline"])


def intent_operations(value):
    validate_intent(value)
    data = value["data"]
    if value["kind"] in {"agent", "review"}:
        return [data["session"]["manifest"]["operation_id"]]
    if value["kind"] == "validation":
        return [entry["manifest"]["operation_id"] for entry in data["operations"]]
    if value["kind"] == "dependency":
        return [data["build_id"]]
    return []


@checked
def receipt_summary(observed):
    if (
        observed.get("state") != "exited"
        or observed.get("descendants_terminated") is not True
    ):
        raise NativeContractError("Native termination remains unconfirmed.")
    result = observed.get("result")
    if not isinstance(result, dict):
        raise NativeContractError("Native operation has no protected result.")
    if result.get("terminated") is not True:
        raise NativeContractError("Protected native termination is unconfirmed.")
    identifier(observed.get("operation_id"))
    if result.get("operation_id") != observed["operation_id"]:
        raise NativeContractError("Protected result operation differs from its status.")
    identifier(result.get("run_id"))
    artifact = result.get("artifact") or {}
    value = {
        "operation_id": observed["operation_id"],
        "run_id": result["run_id"],
        "state": "exited",
        "descendants_terminated": True,
        "container_id": observed.get("container_id", ""),
        "receipt_digest": digest(result),
        "exit_code": result.get("exit_code"),
        "timed_out": result.get("timed_out", False),
        "canceled": result.get("canceled", False),
        "log_truncated": result.get("log_truncated", False),
        "started_at": result.get("started_at"),
        "ended_at": result.get("ended_at"),
        "log_digest": result.get("log_digest", ""),
        "source_digest": artifact.get("source_digest", ""),
        "export_digest": artifact.get("result_digest", ""),
        "inventory_digest": digest(artifact) if artifact else "",
        "error": bool(result.get("error") or result.get("export_error")),
    }
    validate_receipt(value)
    return value


@checked
def validate_receipt(value):
    fields(
        value,
        {
            "operation_id",
            "run_id",
            "state",
            "descendants_terminated",
            "container_id",
            "receipt_digest",
            "exit_code",
            "timed_out",
            "canceled",
            "log_truncated",
            "started_at",
            "ended_at",
            "log_digest",
            "source_digest",
            "export_digest",
            "inventory_digest",
            "error",
        },
    )
    identifier(value["operation_id"])
    identifier(value["run_id"])
    if value["state"] != "exited" or value["descendants_terminated"] is not True:
        raise NativeContractError("Native receipt is not terminal.")
    sha(value["container_id"], empty=True)
    sha(value["receipt_digest"])
    for key in ("log_digest", "source_digest", "export_digest", "inventory_digest"):
        sha(value[key], empty=True)
    for key in ("timed_out", "canceled", "log_truncated", "error"):
        if type(value[key]) is not bool:
            raise NativeContractError("Native receipt flags must be booleans.")
    if value["exit_code"] is not None and type(value["exit_code"]) is not int:
        raise NativeContractError("Native exit code must be an actual integer.")
    for key in ("started_at", "ended_at"):
        entry = value[key]
        if entry is not None and (
            type(entry) not in {int, float} or not math.isfinite(entry) or entry <= 0
        ):
            raise NativeContractError(
                "Native receipt timestamps must be finite and positive."
            )
    if (
        value["started_at"] is not None
        and value["ended_at"] is not None
        and value["started_at"] > value["ended_at"]
    ):
        raise NativeContractError("Native receipt timestamps are unordered.")
    return value


def receipt_passed(value):
    validate_receipt(value)
    return (
        value["exit_code"] == 0
        and type(value["exit_code"]) is int
        and not any(
            value[key] for key in ("timed_out", "canceled", "log_truncated", "error")
        )
        and all(
            value[key]
            for key in (
                "started_at",
                "ended_at",
                "log_digest",
                "source_digest",
                "export_digest",
                "inventory_digest",
                "container_id",
            )
        )
    )


@checked
def agent_result(*, intent_id, session, outcome, observed=None):
    selected = session.to_data() if hasattr(session, "to_data") else session
    result = outcome["result"]
    if observed is None:
        if outcome["successful"]:
            raise NativeContractError(
                "Agent success requires its actual terminal container status."
            )
        observed = {
            "state": "exited",
            "descendants_terminated": True,
            "operation_id": selected["manifest"]["operation_id"],
            "result": result,
        }
    if observed.get("operation_id") != selected["manifest"][
        "operation_id"
    ] or canonical(observed.get("result")) != canonical(result):
        raise NativeContractError(
            "Agent outcome differs from its terminal container status."
        )
    receipt = receipt_summary(observed)
    if outcome["successful"] and not receipt["container_id"]:
        raise NativeContractError(
            "Agent success requires its original container identity."
        )
    artifact = outcome["artifact"]
    if canonical(artifact) != canonical(result.get("artifact")):
        raise NativeContractError("Agent export differs from its protected result.")
    if hashlib.sha256(outcome["source"]).hexdigest() != artifact["result_digest"]:
        raise NativeContractError(
            "Agent export bytes differ from its protected digest."
        )
    usage = [
        {"name": name, "value": amount, "unit": outcome.get("usage_unit", "")}
        for name, amount in outcome.get("usage", {}).items()
    ]
    value = {
        "schema_version": 1,
        "intent_id": intent_id,
        "receipt": receipt,
        "successful": outcome["successful"],
        "session_id": outcome["session_id"],
        "agent_version": outcome["agent_version"],
        "usage": usage,
    }
    return validate_native_artifact("native_agent", value)


@checked
def validation_result(*, intent_id, session, operations, provenance=None, result=None):
    profile = profile_from_data(session["profile"])
    by_id = {value["operation_id"]: value for value in operations}
    checks = [
        {
            "id": entry["id"],
            "receipt": receipt_summary(by_id[entry["manifest"]["operation_id"]]),
        }
        for entry in session["operations"]
    ]
    passed = all(receipt_passed(entry["receipt"]) for entry in checks)
    value = {
        "schema_version": 1,
        "intent_id": intent_id,
        "run_id": session["run_id"],
        "tree_sha": session["tree_sha"],
        "source_digest": session["source_digest"],
        "profile": profile_reference(profile),
        "checks": checks,
        "state": "passed" if passed else "failed",
        "provenance": provenance,
    }
    if passed and (
        not isinstance(result, dict)
        or result.get("state") != "passed"
        or result.get("validation") != validation_evidence(value)
    ):
        raise NativeContractError(
            "Passing validation requires the verified SDK outcome."
        )
    return validate_native_artifact("native_validation", value)


@checked
def workspace_record(
    *,
    context_id,
    context_digest,
    attempt,
    agent_intent_id,
    workspace,
    source_digest,
    export_digest,
    requirements_digest,
):
    return validate_native_artifact(
        "native_workspace",
        {
            "schema_version": 1,
            "context_id": context_id,
            "context_digest": context_digest,
            "attempt": attempt,
            "agent_intent_id": agent_intent_id,
            "workspace": workspace,
            "source_digest": source_digest,
            "export_digest": export_digest,
            "requirements_digest": requirements_digest,
        },
    )


@checked
def delivery_record(
    *,
    intent_ids,
    workspace_id,
    publication_id,
    workspace,
    pushed_sha,
    pull_request=None,
):
    return validate_native_artifact(
        "native_delivery",
        {
            "schema_version": 1,
            "intent_ids": intent_ids,
            "workspace_id": workspace_id,
            "publication_id": publication_id,
            "workspace": workspace,
            "pushed_sha": pushed_sha,
            "pull_request": pull_request,
        },
    )


def pull(value, workspace=None):
    fields(value, {"repository_id", "number", "url", "base", "head", "head_sha"})
    positive(value["repository_id"])
    positive(value["number"])
    branch(value["base"])
    branch(value["head"])
    sha(value["head_sha"], git=True)
    text(value["url"], 500)
    if workspace is not None:
        expected = {
            "repository_id": workspace["repository_id"],
            "base": workspace["base_branch"],
            "head": workspace["branch"],
            "head_sha": workspace["commit_sha"],
            "url": f"https://github.com/{workspace['repository']}/pull/{value['number']}",
        }
        if any(value[key] != entry for key, entry in expected.items()):
            raise NativeContractError("Native pull request differs from its workspace.")
    return value


def unique_ids(value):
    if not isinstance(value, list) or len(value) > 128:
        raise NativeContractError("Native identity lists must be bounded.")
    for entry in value:
        identifier(entry)
    if len(set(value)) != len(value):
        raise NativeContractError("Native identities must be unique.")


@checked
def termination_record(
    *, intent_ids, operations, local_processes, publications, builds=()
):
    if isinstance(local_processes, dict):
        local_processes = [
            {
                "operation_id": key,
                "boot_id": entry["boot"],
                "pid": entry.get("pid"),
                "start": entry.get("start"),
                "status": entry["status"],
                "returncode": entry.get("returncode"),
            }
            for key, entry in local_processes.items()
        ]
    return validate_native_artifact(
        "native_termination",
        {
            "schema_version": 1,
            "intent_ids": sorted(intent_ids),
            "operations": sorted(
                [receipt_summary(entry) for entry in operations],
                key=lambda entry: entry["operation_id"],
            ),
            "local_processes": local_processes,
            "publications": publications,
            "builds": list(builds),
        },
    )


@checked
def validation_evidence(value):
    checks = []
    for entry in value["checks"]:
        receipt = entry["receipt"]
        if not receipt_passed(receipt):
            raise NativeContractError("Native delivery requires passing validation.")
        checks.append(
            {
                "id": entry["id"],
                "operation_id": receipt["operation_id"],
                "exit_code": 0,
                "timed_out": False,
                "started_at": datetime.fromtimestamp(
                    receipt["started_at"], timezone.utc
                ).isoformat(),
                "finished_at": datetime.fromtimestamp(
                    receipt["ended_at"], timezone.utc
                ).isoformat(),
                "log_reference": f"executor:{receipt['operation_id']}:logs",
                "log_digest": receipt["log_digest"],
            }
        )
    return {
        "profile_id": value["profile"]["profile_id"],
        "profile_digest": value["profile"]["profile_digest"],
        "image_digest": value["profile"]["image"],
        "tree_sha": value["tree_sha"],
        "checks": checks,
    }


@checked
def validate_native_artifact(kind, value):
    if kind == "native_intent":
        return validate_intent(value)
    if (
        not isinstance(value, dict)
        or type(value.get("schema_version")) is not int
        or value["schema_version"] != 1
    ):
        raise NativeContractError("Supply version1 native metadata.")
    if kind == "native_agent":
        fields(
            value,
            {
                "schema_version",
                "intent_id",
                "receipt",
                "successful",
                "session_id",
                "agent_version",
                "usage",
            },
        )
        identifier(value["intent_id"])
        validate_receipt(value["receipt"])
        if type(value["successful"]) is not bool:
            raise NativeContractError("Native success must be a boolean.")
        text(value["session_id"], empty=not value["successful"])
        text(value["agent_version"], 200)
        if value["successful"] and not receipt_passed(value["receipt"]):
            raise NativeContractError(
                "Agent success requires protected exit/export proof."
            )
        if not isinstance(value["usage"], list) or len(value["usage"]) > 64:
            raise NativeContractError("Native usage must be a bounded list.")
        for entry in value["usage"]:
            fields(entry, {"name", "value", "unit"})
            text(entry["name"], 100)
            text(entry["unit"], 100)
            amount = entry["value"]
            if (
                type(amount) not in {int, float}
                or not math.isfinite(amount)
                or amount < 0
            ):
                raise NativeContractError("Native usage requires actual finite values.")
    elif kind == "native_workspace":
        fields(
            value,
            {
                "schema_version",
                "context_id",
                "context_digest",
                "attempt",
                "agent_intent_id",
                "workspace",
                "source_digest",
                "export_digest",
                "requirements_digest",
            },
        )
        for key in ("context_id", "agent_intent_id"):
            identifier(value[key])
        for key in (
            "context_digest",
            "source_digest",
            "export_digest",
            "requirements_digest",
        ):
            sha(value[key])
        if type(value["attempt"]) is not int or not 0 <= value["attempt"] <= 2:
            raise NativeContractError("Native work permits at most2 repairs.")
        validate_workspace(value["workspace"])
    elif kind == "native_validation":
        fields(
            value,
            {
                "schema_version",
                "intent_id",
                "run_id",
                "tree_sha",
                "source_digest",
                "profile",
                "checks",
                "state",
                "provenance",
            },
        )
        identifier(value["intent_id"])
        identifier(value["run_id"])
        sha(value["tree_sha"], git=True)
        sha(value["source_digest"])
        selected = profile_from_reference(value["profile"])
        from vibearr_executor.validation_profile import (
            CHECK_IDS,
            validate_dependency_provenance,
        )

        if not isinstance(value["checks"], list) or [
            entry.get("id") for entry in value["checks"]
        ] != list(CHECK_IDS):
            raise NativeContractError(
                "Native validation requires every fixed check in order."
            )
        operations = []
        for entry in value["checks"]:
            fields(entry, {"id", "receipt"})
            validate_receipt(entry["receipt"])
            if entry["receipt"]["run_id"] != value["run_id"]:
                raise NativeContractError("Validation receipt belongs to another run.")
            operations.append(entry["receipt"]["operation_id"])
        unique_ids(operations)
        if value["state"] not in {"passed", "failed"}:
            raise NativeContractError("Native validation must be terminal.")
        if value["state"] == "passed":
            validation_evidence(value)
        if value["provenance"] is not None:
            validate_dependency_provenance(
                value["provenance"], selected, run_id=value["run_id"]
            )
    elif kind == "native_delivery":
        fields(
            value,
            {
                "schema_version",
                "intent_ids",
                "workspace_id",
                "publication_id",
                "workspace",
                "pushed_sha",
                "pull_request",
            },
        )
        unique_ids(value["intent_ids"])
        if not value["intent_ids"]:
            raise NativeContractError("Native delivery requires publication intent.")
        identifier(value["workspace_id"])
        identifier(value["publication_id"])
        validate_workspace(value["workspace"])
        if value["pushed_sha"] != value["workspace"]["commit_sha"]:
            raise NativeContractError(
                "Published ref differs from the inspected commit."
            )
        if value["pull_request"] is not None:
            pull(value["pull_request"], value["workspace"])
    elif kind == "native_termination":
        validate_termination(value)
    elif kind == "native_revision":
        fields(
            value,
            {
                "schema_version",
                "intent_id",
                "context_id",
                "context_digest",
                "attempt",
                "previous_workspace_id",
                "agent_intent_id",
                "workspace",
                "source_digest",
                "export_digest",
                "requirements_digest",
                "parents",
            },
        )
        for key in (
            "intent_id",
            "context_id",
            "previous_workspace_id",
            "agent_intent_id",
        ):
            identifier(value[key])
        for key in (
            "context_digest",
            "source_digest",
            "export_digest",
            "requirements_digest",
        ):
            sha(value[key])
        if type(value["attempt"]) is not int or not 1 <= value["attempt"] <= 2:
            raise NativeContractError(
                "Publication revisions consume the shared repair allowance."
            )
        validate_workspace(value["workspace"])
        if not isinstance(value["parents"], list) or len(value["parents"]) != 2:
            raise NativeContractError(
                "Base update requires both immutable merge parents."
            )
        for parent in value["parents"]:
            sha(parent, git=True)
        if value["parents"][1] != value["workspace"]["base_revision"]:
            raise NativeContractError("Base revision differs from the merge parent.")
    elif kind == "native_merge":
        from .merge_contract import merge_receipt

        fields(
            value,
            {
                "schema_version",
                "intent_id",
                "workspace_id",
                "publication_id",
                "workspace",
                "pull_request",
                "receipt",
                "merge",
            },
        )
        for key in ("intent_id", "workspace_id", "publication_id"):
            identifier(value[key])
        validate_workspace(value["workspace"])
        pull(value["pull_request"], value["workspace"])
        fields(value["merge"], {"commit_sha", "head_sha", "merged_at"})
        sha(value["merge"]["commit_sha"], git=True)
        timestamp(value["merge"]["merged_at"])
        if value["merge"]["head_sha"] != value["workspace"]["commit_sha"]:
            raise NativeContractError(
                "Merged head differs from the validated publication."
            )
        merge_receipt(value["receipt"], None, value["workspace"])
    elif kind == "native_completion":
        fields(
            value,
            {
                "schema_version",
                "intent_ids",
                "merge_intent_id",
                "issue_number",
                "issue_id",
                "comment_id",
                "marker",
                "state",
                "branch_deleted",
            },
        )
        unique_ids(value["intent_ids"])
        if not value["intent_ids"]:
            raise NativeContractError(
                "Completion requires registered reporting intents."
            )
        identifier(value["merge_intent_id"])
        for key in ("issue_number", "issue_id", "comment_id"):
            positive(value[key])
        text(value["marker"], 200)
        if value["state"] != "closed" or type(value["branch_deleted"]) is not bool:
            raise NativeContractError(
                "Issue completion must record the actual closed state."
            )
    elif kind == "native_merge_terminal":
        fields(value, {"schema_version", "intent_id", "state"})
        identifier(value["intent_id"])
        if value["state"] != "not_started":
            raise NativeContractError(
                "Only a definitely unstarted merge mutation is terminal without its provider fact."
            )
    elif kind in {
        "native_review",
        "native_review_publication",
        "native_review_publication_terminal",
    }:
        from .review_contract import validate_review_artifact

        validate_review_artifact(kind, value)
    else:
        raise NativeContractError("Unknown native artifact kind.")
    return copied(value)


@checked
def validate_termination(value):
    fields(
        value,
        {
            "schema_version",
            "intent_ids",
            "operations",
            "local_processes",
            "publications",
            "builds",
        },
    )
    unique_ids(value["intent_ids"])
    if not isinstance(value["operations"], list) or len(value["operations"]) > 128:
        raise NativeContractError("Native termination operations must be bounded.")
    operations = []
    for entry in value["operations"]:
        validate_receipt(entry)
        operations.append(entry["operation_id"])
    unique_ids(operations)
    processes = value["local_processes"]
    if not isinstance(processes, list) or len(processes) > 1000:
        raise NativeContractError("Native local process list must be bounded.")
    identities = []
    for entry in processes:
        fields(
            entry, {"operation_id", "boot_id", "pid", "start", "status", "returncode"}
        )
        identifier(entry["operation_id"])
        identifier(entry["boot_id"])
        if entry["status"] != "stopped":
            raise NativeContractError(
                "Native local child termination remains unconfirmed."
            )
        for key in ("pid", "start"):
            if entry[key] is not None:
                positive(entry[key])
        if entry["returncode"] is not None and type(entry["returncode"]) is not int:
            raise NativeContractError("Native child exit code must be an integer.")
        identities.append(entry["operation_id"])
    if len(set(identities)) != len(identities):
        raise NativeContractError("Native local process identities must be unique.")
    publications = value["publications"]
    if not isinstance(publications, list) or len(publications) > 128:
        raise NativeContractError("Native publication termination must be bounded.")
    for entry in publications:
        fields(
            entry,
            {
                "intent_id",
                "publication_id",
                "stage",
                "state",
                "pushed_sha",
                "pull_request",
            },
        )
        identifier(entry["intent_id"])
        identifier(entry["publication_id"])
        if entry["stage"] not in {"push", "create_pr"} or entry["state"] not in {
            "not_started",
            "known_push",
            "known_pr",
        }:
            raise NativeContractError("Native publication outcome is still ambiguous.")
        sha(entry["pushed_sha"], git=True, empty=True)
        if entry["state"] == "not_started" and (
            entry["pushed_sha"] or entry["pull_request"]
        ):
            raise NativeContractError(
                "Unstarted publication cannot claim external effects."
            )
        if entry["state"] == "known_push" and (
            not entry["pushed_sha"] or entry["pull_request"]
        ):
            raise NativeContractError("Known push requires its observed ref.")
        if entry["state"] == "known_pr":
            pull(entry["pull_request"])
            if entry["pull_request"]["head_sha"] != entry["pushed_sha"]:
                raise NativeContractError("Known PR differs from the observed push.")
    builds = value["builds"]
    if not isinstance(builds, list) or len(builds) > 16:
        raise NativeContractError("Native build termination must be bounded.")
    for entry in builds:
        fields(
            entry,
            {"build_id", "run_id", "state", "resources_terminated", "receipt_digest"},
        )
        identifier(entry["build_id"])
        identifier(entry["run_id"])
        sha(entry["receipt_digest"])
        if (
            entry["state"] not in {"ready", "canceled"}
            or entry["resources_terminated"] is not True
        ):
            raise NativeContractError(
                "Native dependency qualification still owns resources."
            )


@checked
def status_document(
    *,
    sequence,
    stage,
    wait_reason="",
    detail="",
    repair_count=0,
    validation_status="not_started",
    session_id="",
):
    value = {
        "sequence": sequence,
        "stage": stage,
        "wait_reason": wait_reason,
        "detail": detail,
        "repair_count": repair_count,
        "validation_status": validation_status,
        "session_id": session_id,
    }
    if type(sequence) is not int or not 0 < sequence < 2**63:
        raise NativeContractError("Native status requires a positive sequence.")
    if stage not in {
        "claimed",
        "context",
        "research",
        "planning",
        "implementation",
        "validation",
        "delivery",
        "waiting",
    }:
        raise NativeContractError("Native status stage is invalid.")
    if type(repair_count) is not int or not 0 <= repair_count <= 2:
        raise NativeContractError("Native status repair count exceeds its bound.")
    if validation_status not in {
        "not_started",
        "running",
        "passed",
        "failed",
        "canceled",
        "waiting",
    }:
        raise NativeContractError("Native validation status is invalid.")
    text(wait_reason, 2000, empty=True)
    text(detail, 4000, empty=True)
    text(session_id, 2000, empty=True)
    return copied(value, 8192)
