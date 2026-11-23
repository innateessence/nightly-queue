"""Resumable factory acceptance; humans alone merge the resulting PRs."""

import argparse
from copy import deepcopy
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
from uuid import UUID, uuid4

from .bootstrap_files import private_json
from .client import APIError
from .delivery import DeliveryError
from .native_contract import (
    digest,
    receipt_passed,
    receipt_summary,
    validate_native_artifact,
)
from .native_evidence import digest as valid_digest
from .merge_github import MergeGitHub, MergeRepair, MergeWait
from .native_workspace import atomic_bytes
from .self_acceptance import collect_run, evidence_entries, timestamp
from .workspaces import private_directory, validate_branch


RUNTIME = "compact-worker-v1"
SERVICES = {"app", "postgres", "runner", "worker"}
REVIEWERS = {"reviewer-code", "reviewer-security", "reviewer-ux", "reviewer-ui"}
FAILURE_CASES = {
    "failed-check-repair",
    "review-request-repair",
    "repair-exhaustion",
    "missing-capability",
    "cancellation",
    "controller-restart",
    "worker-restart",
    "independent-work-during-human-review",
}
MAX_ARTIFACTS = 1000


def require(condition, message):
    if not condition:
        raise ValueError(message)


def positive(value):
    require(
        type(value) is int and 0 < value < 2**63, "Use positive numeric identities."
    )
    return value


def identifier(value):
    require(
        isinstance(value, str) and str(UUID(value)) == value,
        "Use canonical UUID identities.",
    )
    return value


def scenario_document(value):
    require(
        isinstance(value, dict)
        and type(value.get("schema_version")) is int
        and value["schema_version"] == 1,
        "Unsupported factory acceptance scenario.",
    )
    for key in ("project_id", "repository_id"):
        positive(value.get(key))
    require(
        isinstance(value.get("repository"), str)
        and re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value["repository"]),
        "Select one GitHub repository.",
    )
    validate_branch(value.get("base_branch"))
    require(bool(value["base_branch"]), "Select the configured base branch.")
    require(
        value.get("provider") in {"codex", "claude", "opencode"},
        "Select a native provider.",
    )
    require(
        isinstance(value.get("agent_version"), str) and bool(value["agent_version"]),
        "Pin the selected native CLI version.",
    )
    positive(value.get("connection_id"))
    valid_digest(value.get("configuration_revision"))
    valid_digest(value.get("source_revision"), git=True)
    tasks = value.get("tasks")
    require(
        isinstance(tasks, list) and len(tasks) == 2,
        "Freeze exactly two new source-level tasks.",
    )
    for task in tasks:
        positive(task.get("issue_number"))
        valid_digest(task.get("body_digest"))
        criteria = task.get("acceptance_criteria")
        require(
            isinstance(criteria, list)
            and bool(criteria)
            and all(
                isinstance(entry, str) and 0 < len(entry) <= 2000 for entry in criteria
            ),
            "Freeze testable, unmet acceptance criteria before execution.",
        )
        surfaces = task.get("surfaces", [])
        require(
            isinstance(surfaces, list) and set(surfaces) <= {"ui", "security"},
            "Use known acceptance surfaces.",
        )
    require(
        tasks[0]["issue_number"] != tasks[1]["issue_number"],
        "Tasks must be distinct issues.",
    )
    require(
        type(value.get("repair_limit")) is int and 0 <= value["repair_limit"] <= 2,
        "Acceptance permits at most two shared repairs.",
    )
    checks = value.get("required_checks")
    require(
        isinstance(checks, list) and 1 <= len(checks) <= 32,
        "Freeze the required CI producers.",
    )
    for check in checks:
        for field in ("app_id", "workflow_id"):
            positive(check.get(field))
        valid_digest(check.get("workflow_digest"))
        require(
            isinstance(check.get("name"), str)
            and bool(check["name"])
            and isinstance(check.get("workflow_path"), str)
            and re.fullmatch(
                r"\.github/workflows/[A-Za-z0-9_.-]+\.ya?ml", check["workflow_path"]
            ),
            "Freeze a named check and fixed workflow path.",
        )
    require(
        len({check["name"] for check in checks}) == len(checks),
        "Required CI checks must be unique.",
    )
    require(
        len(json.dumps(value, allow_nan=False).encode()) <= 65536,
        "Scenario exceeds its limit.",
    )
    return deepcopy(value)


class State:
    def __init__(self, directory):
        self.directory = private_directory(Path(directory))
        self.path = self.directory / "factory-acceptance.json"
        self.lock = None

    def __enter__(self):
        self.lock = os.open(
            self.directory / "factory-acceptance.lock",
            os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW,
            0o600,
        )
        fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return self

    def __exit__(self, *_args):
        if self.lock is not None:
            os.close(self.lock)

    def load(self):
        value = private_json(self.path)
        identifier(value.get("operation_id"))
        require(value.get("schema_version") == 1, "Unsupported acceptance state.")
        scenario = scenario_document(value.get("scenario"))
        require(
            value.get("scenario_digest") == digest(scenario), "Frozen scenario changed."
        )
        require(
            value.get("evidence_kind") in {"fixture", "live"},
            "Unknown acceptance evidence kind.",
        )
        require(
            isinstance(value.get("runs"), list)
            and len(value["runs"]) == 2
            and isinstance(value.get("snapshots"), dict),
            "Acceptance run checkpoints are invalid.",
        )
        for run_id in value["runs"]:
            if run_id is not None:
                identifier(run_id)
        return value

    def save(self, value):
        content = json.dumps(value, sort_keys=True, allow_nan=False).encode()
        require(len(content) <= 8 * 1024**2, "Acceptance state exceeds its bound.")
        atomic_bytes(self.path, content)


def prepare(directory, scenario, *, evidence_kind="fixture"):
    scenario = scenario_document(scenario)
    require(evidence_kind in {"fixture", "live"}, "Choose fixture or live evidence.")
    with State(directory) as storage:
        if storage.path.exists():
            value = storage.load()
            require(
                value["scenario"] == scenario
                and value["evidence_kind"] == evidence_kind,
                "Resume the original scenario and evidence kind.",
            )
            return value
        value = {
            "schema_version": 1,
            "operation_id": str(uuid4()),
            "scenario": scenario,
            "scenario_digest": digest(scenario),
            "evidence_kind": evidence_kind,
            "stage": "prepared",
            "runs": [None, None],
            "snapshots": {},
            "report": None,
        }
        storage.save(value)
        return value


class ReadOnlyQueue:
    def __init__(self, client):
        self.client = client

    def request(self, method, path, payload=None, **kwargs):
        require(
            method == "GET" and payload is None,
            "Acceptance collection only reads the queue.",
        )
        return self.client.request(method, path, **kwargs)

    def reconcile(self, run_id):
        return self.request("GET", f"/api/v1/runner/runs/{identifier(run_id)}/")

    def active(self):
        return self.request("GET", "/api/v1/runner/runs/active/")


def factory_artifacts(queue, run_id):
    from .factory_contract import validate_artifact

    values, after = [], 0
    for _ in range(101):
        page = queue.request(
            "GET",
            f"/api/v1/runner/runs/{identifier(run_id)}/factory/artifacts/",
            query={"after": after},
        )
        entries = page.get("artifacts")
        require(
            isinstance(entries, list) and len(entries) <= 10,
            "Invalid factory artifact page.",
        )
        for entry in entries:
            sequence = entry.get("sequence")
            require(
                type(sequence) is int and sequence > after,
                "Factory cursor did not advance.",
            )
            identifier(entry.get("id"))
            detail = queue.request(
                "GET", f"/api/v1/runner/runs/{run_id}/factory/artifacts/{entry['id']}/"
            ).get("artifact")
            fields = (
                "id",
                "sequence",
                "digest",
                "operation_id",
                "role",
                "round",
                "context_id",
                "specification_id",
            )
            require(
                isinstance(detail, dict)
                and not detail.get("unavailable")
                and not detail.get("presentation_only")
                and all(detail.get(field) == entry.get(field) for field in fields),
                "Artifact detail differs from its immutable list identity.",
            )
            document = validate_artifact(detail.get("document"))
            require(
                document["run_id"] == run_id
                and entry.get("digest") == digest(document),
                "Artifact identity or immutable digest differs.",
            )
            require(
                all(document.get(field) == detail.get(field) for field in fields[3:]),
                "Artifact metadata differs from its document.",
            )
            require(
                entry["id"] not in {value["id"] for value in values},
                "Duplicate artifact identity.",
            )
            values.append(detail)
            after = sequence
            require(len(values) <= MAX_ARTIFACTS, "Factory history exceeds its bound.")
        if page.get("has_more") is False:
            return values
        require(
            bool(entries)
            and page.get("has_more") is True
            and page.get("next_after") == after,
            "Factory pagination is incomplete.",
        )
    raise ValueError("Factory history exceeds bounded pagination.")


def immutable_document(queue, run_id, kind, document_id):
    identifier(document_id)
    singular = "context" if kind == "contexts" else "plan"
    value = queue.request("GET", f"/api/v1/runner/runs/{run_id}/{kind}/{document_id}/")[
        singular
    ]
    require(
        isinstance(value, dict)
        and value.get("id") == document_id
        and not value.get("presentation_only")
        and not value.get("unavailable")
        and isinstance(value.get("document"), dict)
        and digest(value["document"]) == value.get("digest"),
        "Saved context or plan is unavailable or differs from its immutable digest.",
    )
    return value


def factory_documents(queue, run, entries):
    contexts = {entry["document"]["context_id"] for entry in entries}
    plans = {
        entry["document"]["specification_id"]
        for entry in entries
        if entry["document"]["specification_id"]
    }
    contexts.add(run["context_reference"])
    plans.add(run["plan_reference"])
    require(
        len(contexts) <= 3 and len(plans) <= 3,
        "Factory generations exceed the shared repair budget.",
    )
    documents = {
        "contexts": {
            key: immutable_document(queue, run["id"], "contexts", key)
            for key in contexts
        },
        "plans": {
            key: immutable_document(queue, run["id"], "plans", key) for key in plans
        },
    }
    verify_generations(documents["contexts"], run)
    return documents


def verify_generations(contexts, run):
    current = run["context_reference"]
    seen = set()
    while current is not None:
        require(
            current not in seen and current in contexts,
            "Factory context generations are cyclic or incomplete.",
        )
        seen.add(current)
        context = contexts[current]["document"]
        generation = context.get("factory_generation")
        if generation is None:
            break
        fields = {
            "previous_context_id",
            "previous_context_digest",
            "previous_workspace_id",
            "attempt",
            "old_base",
            "new_base",
            "reason",
        }
        require(
            isinstance(generation, dict)
            and set(generation) == fields
            and generation["reason"] == "base_update",
            "Unsupported factory base-generation binding.",
        )
        attempt = generation["attempt"]
        require(
            type(attempt) is int
            and 1
            <= attempt
            <= run["configuration_snapshot"]["execution_policy"]["repair_limit"],
            "Base generations exceeded the shared repair budget.",
        )
        identifier(generation["previous_workspace_id"])
        previous_id = identifier(generation["previous_context_id"])
        require(
            previous_id in contexts
            and contexts[previous_id]["digest"]
            == generation["previous_context_digest"],
            "Base generation changed its previous immutable context.",
        )
        previous = contexts[previous_id]["document"]
        require(
            context["repository"]["base_revision"] == generation["new_base"]
            and previous["repository"]["base_revision"] == generation["old_base"],
            "Base generation does not identify the inspected old and new base.",
        )
        mutable = {"source_digest", "fetched_at", "factory_generation", "repository"}
        require(
            {key: value for key, value in context.items() if key not in mutable}
            == {key: value for key, value in previous.items() if key not in mutable},
            "Base generation changed frozen task, policy, account or role authority.",
        )
        old_attempt = previous.get("factory_generation", {}).get("attempt", 0)
        require(old_attempt < attempt, "Base generation reused a spent repair attempt.")
        current = previous_id
    require(
        seen == set(contexts),
        "Factory evidence contains an unlinked context generation.",
    )


def criterion_texts(values):
    require(
        isinstance(values, list) and bool(values), "Missing frozen acceptance criteria."
    )
    result = [
        (
            value.get("description", value.get("text"))
            if isinstance(value, dict)
            else value
        )
        for value in values
    ]
    require(
        all(isinstance(value, str) and bool(value) for value in result),
        "Invalid frozen acceptance criterion text.",
    )
    return set(result)


def verify_task_requirements(
    entries, run, collected, scenario, context, plan, authority
):
    selected = [
        task
        for task in scenario["tasks"]
        if task["issue_number"] == collected["issue_number"]
    ]
    require(
        len(selected) == 1, "Factory run differs from the selected acceptance issue."
    )
    task = selected[0]
    source = context["document"]["source"]
    require(
        source == run["source_snapshot"]
        and digest(source.get("body")) == task["body_digest"],
        "Frozen native context did not execute the selected acceptance issue body.",
    )
    repository = context["document"]["repository"]
    workspace = collected["delivery"]["workspace"]
    require(
        context["document"]["run_id"] == run["id"]
        and all(
            repository.get(key) == workspace.get(key)
            for key in ("repository_id", "repository", "base_branch", "base_revision")
        ),
        "Frozen native context belongs to another run, repository or inspected base.",
    )
    current = [
        entry for entry in entries if entry["document"]["context_id"] == context["id"]
    ]
    intakes = [
        entry["document"] for entry in current if entry["document"]["role"] == "intake"
    ]
    require(
        bool(intakes) and intakes[-1]["payload"]["verdict"] == "accept",
        "Missing accepted current intake.",
    )
    specification = next(
        entry["document"]
        for entry in current
        if entry["id"] == plan["document"]["factory_specification_id"]
    )
    criteria = set(task["acceptance_criteria"])
    require(
        criteria <= criterion_texts(intakes[-1]["payload"]["criteria"])
        and criteria <= criterion_texts(specification["payload"]["criteria"]),
        "Current intake or specification dropped a frozen acceptance criterion.",
    )
    routing = next(
        entry["document"]["payload"]
        for entry in current
        if entry["document"]["role"] == "routing"
        and entry["document"]["round"] == authority["round"]
    )
    surfaces = set(task.get("surfaces", []))
    require(
        surfaces <= set(intakes[-1]["payload"].get("surfaces", []))
        and surfaces <= set(routing.get("surfaces", [])),
        "Current intake or routing dropped a frozen acceptance surface.",
    )
    required = {"reviewer-code"}
    if "security" in surfaces:
        required.add("reviewer-security")
    if "ui" in surfaces:
        required.update({"reviewer-ux", "reviewer-ui"})
    require(
        required <= set(authority["required_reviewers"]),
        "Frozen acceptance surfaces lack their required reviewers.",
    )


def check_factory_chain(entries, native_entries, run, collected, scenario, documents):
    from .factory_delivery import checked_factory_workspace

    workspace = collected["delivery"]["workspace"]
    require(
        run["configuration_revision"] == scenario["configuration_revision"],
        "Run configuration differs from the frozen scenario.",
    )
    policy = run["configuration_snapshot"]["execution_policy"]
    require(
        policy["delivery_mode"] == "pull_request"
        and policy["repair_limit"] == scenario["repair_limit"],
        "Frozen delivery or repair policy differs.",
    )
    artifacts = [entry["document"] for entry in entries]
    require(bool(artifacts), "No factory artifacts were saved.")
    rounds = sorted({document["round"] for document in artifacts})
    require(
        rounds == list(range(1, max(rounds) + 1))
        and max(rounds) <= policy["repair_limit"] + 1,
        "Factory rounds are missing or exceed the shared repair budget.",
    )
    require(
        all(
            document["configuration_digest"] == digest(run["configuration_snapshot"])
            for document in artifacts
        ),
        "Artifact configuration binding changed.",
    )
    for document in artifacts:
        context = documents["contexts"][document["context_id"]]
        require(
            context["digest"] == document["context_digest"],
            "Artifact context binding changed.",
        )
        plan_id = document["specification_id"]
        if plan_id is not None:
            plan = documents["plans"][plan_id]
            require(
                plan["digest"] == document["specification_digest"]
                and plan["context_id"] == document["context_id"],
                "Artifact specification binding changed.",
            )
    context = documents["contexts"][run["context_reference"]]
    plan = documents["plans"][run["plan_reference"]]
    authority = checked_factory_workspace(
        run["configuration_snapshot"],
        plan,
        context,
        workspace,
        entries,
        [{**entry, "payload": entry["data"]} for entry in native_entries],
        run["id"],
    )
    require(
        isinstance(authority, dict),
        "Acceptance requires enabled factory publication authority.",
    )
    verify_task_requirements(
        entries, run, collected, scenario, context, plan, authority
    )
    native_by_id = {str(entry["id"]): entry for entry in native_entries}
    native_operations = {entry["operation_id"]: entry for entry in native_entries}
    agent_operations = set()
    agent_receipts = {}
    intervals = []
    for document in artifacts:
        execution = document.get("execution")
        if execution is None:
            continue
        require(
            execution["adapter"] == scenario["provider"]
            and execution["agent_version"] == scenario["agent_version"],
            "Role backend/CLI differs from the selected provider.",
        )
        result = native_by_id.get(execution["result_id"]) or native_operations.get(
            execution["result_id"]
        )
        intent = native_by_id.get(execution["intent_id"]) or native_operations.get(
            execution["intent_id"]
        )
        require(
            result is not None
            and result["kind"] == "native_agent"
            and intent is not None
            and intent["kind"] == "native_intent",
            "Role lacks its saved native execution intent/result.",
        )
        require(
            result["data"]["successful"] is True
            and result["data"]["session_id"] == execution["session_id"]
            and result["data"]["intent_id"] == execution["intent_id"]
            and result["data"]["agent_version"] == execution["agent_version"]
            and result["data"]["receipt"]["operation_id"] == execution["operation_id"]
            and result["data"]["receipt"]["started_at"] == execution["started_at"]
            and result["data"]["receipt"]["ended_at"] == execution["ended_at"]
            and result["data"]["receipt"]["execution_identity"]
            == execution["execution_identity"],
            "Role execution differs from the genuine native result.",
        )
        session = intent["data"]["data"]["session"]
        account = session["account"]
        require(
            account["credential_id"] == scenario["connection_id"],
            "Role used another selected connection.",
        )
        frozen = documents["contexts"][document["context_id"]]["document"]["account"]
        model = run["configuration_snapshot"]["effective"]["factory"]["roles"][
            document["role"]
        ]["model"]
        require(
            account == {**frozen, "model": model}
            and account["backend"] == scenario["provider"],
            "Role account differs from its frozen provider, credential version or configured model.",
        )
        require(
            session["adapter"] == execution["adapter"]
            and account["model"] == execution["model"]
            and session["agent_version"] == execution["agent_version"]
            and session["manifest"]["operation_id"] == execution["operation_id"]
            and session["manifest"]["source_digest"] == execution["input_source_digest"]
            and session["manifest"]["context_digest"] == execution["input_digest"],
            "Role input, model or CLI differs from its frozen native session.",
        )
        receipt = result["data"]["receipt"]
        require(
            receipt["run_id"] == run["id"]
            and session["manifest"]["run_id"] == run["id"]
            and receipt["source_digest"] == execution["input_source_digest"],
            "Role protected input belongs to another run or source.",
        )
        if document["role"] == "build":
            imports = [
                entry["data"]
                for entry in native_entries
                if entry["kind"] == "native_workspace"
                and entry["data"].get("source_digest") == document["source_digest"]
                and entry["data"].get("agent_intent_id") == execution["intent_id"]
                and entry["data"].get("context_id") == document["context_id"]
                and entry["data"].get("context_digest") == document["context_digest"]
                and entry["data"].get("workspace", {}).get("tree_sha")
                == document["tree_sha"]
                and entry["data"].get("workspace", {}).get("commit_sha")
                == document["head_sha"]
                and entry["data"].get("workspace", {}).get("base_revision")
                == document["base_sha"]
            ]
            require(
                bool(imports)
                and imports[-1]["export_digest"] == receipt["export_digest"],
                "Builder output lacks its actual imported native workspace receipt.",
            )
        else:
            require(
                receipt["source_digest"]
                == receipt["export_digest"]
                == document["source_digest"],
                "Read-only role output differs from its inspected candidate source.",
            )
        descriptor = intent["data"]["data"].get("factory")
        expected_dispatch = {
            key: document[key]
            for key in ("role", "round", "specification_id", "specification_digest")
        }
        expected_dispatch.update(
            {key: execution[key] for key in ("prompt_digest", "input_digest")}
        )
        require(
            descriptor == expected_dispatch
            and intent["data"]["context_id"] == document["context_id"]
            and intent["data"]["context_digest"] == document["context_digest"]
            and intent["data"]["attempt"] == document["round"] - 1,
            "Role lacks the exact six-field dispatch, context or shared repair attempt.",
        )
        require(
            execution["execution_identity"]["runtime"] == RUNTIME,
            "Earlier integrated/fleet execution cannot qualify the compact factory.",
        )
        require(
            execution["operation_id"] not in agent_operations,
            "Two roles reused one model operation.",
        )
        agent_operations.add(execution["operation_id"])
        agent_receipts[execution["operation_id"]] = result["data"]["receipt"]
        intervals.append((execution["started_at"], execution["ended_at"]))
    controls = run["configuration_snapshot"].get("global", {})
    allowance = controls.get("implementation_timeout_seconds")
    require(
        type(allowance) in {int, float} and math.isfinite(allowance) and allowance > 0,
        "Missing frozen aggregate role deadline.",
    )
    require(
        sum(end - start for start, end in intervals) <= allowance,
        "Aggregate role time exceeded the frozen agent budget.",
    )
    started, ended = timestamp(run["created_at"]), timestamp(run["finished_at"])
    require(
        all(
            started.timestamp() <= start <= end <= ended.timestamp()
            for start, end in intervals
        ),
        "Role execution time lies outside the actual run.",
    )
    terminations = [
        entry["data"]
        for entry in native_entries
        if entry["kind"] == "native_termination"
    ]
    require(bool(terminations), "Missing confirmed worker termination.")
    stopped = {
        operation["operation_id"]
        for termination in terminations
        for operation in termination["operations"]
    }
    require(
        agent_operations <= stopped, "A role operation lacks confirmed termination."
    )
    terminated = {
        operation["operation_id"]: operation
        for termination in terminations
        for operation in termination["operations"]
    }
    require(
        all(terminated[key] == value for key, value in agent_receipts.items()),
        "Role results differ from their saved confirmed termination receipts.",
    )
    for entry in native_entries:
        if entry["kind"] in {
            "native_merge",
            "native_completion",
            "native_merge_terminal",
        }:
            raise ValueError(
                "Runner merge/completion writes cannot qualify human-merge acceptance."
            )
        if entry["kind"] == "native_intent":
            require(
                entry["data"]["kind"] != "merge", "Runner attempted an automatic merge."
            )
    build = [document for document in artifacts if document["role"] == "build"][-1]
    return {
        **authority,
        "rounds": max(rounds),
        "source_digest": build["source_digest"],
        "artifact_ids": [entry["id"] for entry in entries],
        "operation_ids": sorted(agent_operations),
    }


def observe_human_merge(github, collected):
    workspace = collected["delivery"]["workspace"]
    pull_number = collected["delivery"]["delivery"]["pull_request"]["number"]
    raw = github.pull(workspace["repository"], pull_number)
    require(raw.get("auto_merge") is None, "GitHub auto-merge must remain disabled.")
    require(
        raw["head"]["sha"] == workspace["commit_sha"],
        "PR head changed after factory checks; collect a new reviewed candidate.",
    )
    if raw.get("state") == "open" and raw.get("merged") is False:
        return {"status": "awaiting_human_merge", "pull_number": pull_number}
    require(
        raw.get("state") == "closed" and raw.get("merged") is True,
        "A closed-unmerged PR does not complete the issue dependency.",
    )
    actor = raw.get("merged_by")
    require(
        isinstance(actor, dict)
        and actor.get("type") == "User"
        and type(actor.get("id")) is int
        and actor["id"] > 0
        and isinstance(actor.get("login"), str)
        and actor["login"]
        and not actor["login"].endswith("[bot]"),
        "Merge must identify an actual human actor.",
    )
    valid_digest(raw.get("merge_commit_sha"), git=True)
    require(
        timestamp(raw.get("merged_at")) >= timestamp(collected["finished_at"]),
        "Human merge predates verified delivery.",
    )
    return {
        "status": "human_merged",
        "pull_number": pull_number,
        "actor": actor,
        "merge_sha": raw["merge_commit_sha"],
        "merged_at": raw["merged_at"],
    }


def verify_stack(report, capabilities, source_revision):
    from .compact_evidence import validate_compact_report

    require(
        report.get("source_revision") == source_revision,
        "Engineering evidence belongs to another deployed source revision.",
    )
    return validate_compact_report(report, capabilities)


def verify_native_receipts(entries, worker):
    require(worker is not None, "Authenticated original worker status is required.")
    receipts = {}
    for entry in entries:
        if entry["kind"] != "native_termination":
            continue
        for receipt in entry["data"]["operations"]:
            operation_id = receipt["operation_id"]
            require(
                operation_id not in receipts or receipts[operation_id] == receipt,
                "An original operation has conflicting termination receipts.",
            )
            receipts[operation_id] = receipt
    require(bool(receipts), "Missing authenticated termination receipts.")
    for operation_id, receipt in receipts.items():
        require(
            receipt_summary(worker.status(operation_id)) == receipt,
            "Saved termination differs from the authenticated original worker receipt.",
        )
    return sorted(receipts)


def verify_checks(github, collected, scenario):
    workspace = collected["delivery"]["workspace"]
    number = collected["delivery"]["delivery"]["pull_request"]["number"]
    for check in scenario["required_checks"]:
        original = github.content(
            workspace["repository"], check["workflow_path"], workspace["base_revision"]
        )
        candidate = github.content(
            workspace["repository"], check["workflow_path"], workspace["commit_sha"]
        )
        require(
            hashlib.sha256(original).hexdigest() == check["workflow_digest"]
            and candidate == original,
            "Trusted workflow definition changed or differs from its pinned digest.",
        )
    return github.checks(workspace, number, {"checks": scenario["required_checks"]})


def verify_browser(collected, chain, supplied, worker, capabilities):
    from .browser_receipts import import_worker_capture, validate_worker_report

    require(
        isinstance(supplied, dict) and worker is not None,
        "Missing actual worker browser evidence.",
    )
    report = supplied.get("report", {})
    binding = report.get("worker_binding", {})
    status = worker.status(identifier(binding.get("operation_id")))
    receipt = receipt_summary(status)
    require(
        receipt_passed(receipt)
        and receipt["execution_identity"] == binding.get("execution_identity")
        and receipt["run_id"] == collected["run_id"]
        and receipt["source_digest"]
        == receipt["export_digest"]
        == chain["source_digest"],
        "Browser operation did not pass on its original run, worker and unchanged source.",
    )
    workspace = collected["delivery"]["workspace"]
    image = collected["delivery"]["runtime"]["image_digest"]
    expected = {
        "schema_version": 1,
        "revision": workspace["commit_sha"],
        "tree_sha": workspace["tree_sha"],
        "source_digest": chain["source_digest"],
        "application_image": image,
        "browser_image": image,
    }
    profiles = [
        profile
        for profile in capabilities.get("profiles", [])
        if profile.get("profile_id") == "browser-ui"
    ]
    require(
        len(profiles) == 1
        and binding.get("profile_id") == "browser-ui"
        and binding.get("profile_digest") == profiles[0].get("profile_digest")
        and profiles[0].get("image") == image,
        "Browser capture differs from its registered immutable browser profile.",
    )
    require(
        binding.get("run_id") == collected["run_id"],
        "Browser capture belongs to another run.",
    )
    authenticated_report, archive = worker.browser_result(binding["operation_id"])
    require(
        authenticated_report == report,
        "Supplied browser report differs from the authenticated original export.",
    )
    with tempfile.TemporaryDirectory(prefix="factory-browser-") as temporary:
        import_worker_capture(
            authenticated_report,
            archive,
            binding=expected,
            worker_binding=binding,
            artifact_root=Path(temporary),
        )
    return validate_worker_report(
        report,
        binding=expected,
        worker_binding=binding,
        storage_root=Path(supplied["storage_root"]),
    )


def verify_failures(failures, worker):
    missing = []
    for name in sorted(FAILURE_CASES):
        entry = failures.get(name, {})
        if (
            entry.get("passed") is not True
            or entry.get("fixtures") is not False
            or not entry.get("receipts")
            or worker is None
        ):
            missing.append("Missing actual failure-matrix evidence: " + name)
            continue
        for receipt in entry["receipts"]:
            operation_id = identifier(receipt.get("operation_id"))
            status = worker.status(operation_id)
            require(
                status.get("state") == "exited"
                and status.get("descendants_terminated") is True
                and status.get("receipt_digest") == receipt.get("receipt_digest")
                and status.get("execution_identity")
                == receipt.get("execution_identity"),
                "Failure-matrix receipt does not match the authenticated terminated operation.",
            )
        missing.append(
            "No trusted scenario producer for "
            + name
            + "; authenticated termination alone does not prove this factory behavior."
        )
    return missing


def collect(
    queue,
    github,
    state,
    *,
    stack_report,
    capabilities,
    failure_evidence=None,
    browser_evidence=None,
    worker=None,
):
    require(
        getattr(github, "read_only", False) is True,
        "Use an explicitly read-only GitHub client.",
    )
    queue = ReadOnlyQueue(queue)
    scenario = scenario_document(state["scenario"])
    records = []
    for index, task in enumerate(scenario["tasks"]):
        run_id = state["runs"][index]
        if run_id is None:
            break
        response = queue.reconcile(run_id)
        run = response["run"]
        if run.get("state") != "completed":
            break
        require(
            run.get("native_outcome") == "delivered",
            "Factory acceptance run did not deliver a PR.",
        )
        issue = github.request(
            "GET", f"/repos/{scenario['repository']}/issues/{task['issue_number']}"
        )
        require(
            digest(issue.get("body")) == task["body_digest"],
            "Frozen acceptance issue content changed.",
        )
        if index == 1:
            require(
                re.search(
                    rf"Depends on:\s*#{scenario['tasks'][0]['issue_number']}\b",
                    issue.get("body", ""),
                ),
                "Issue B must declare its actual prerequisite issue.",
            )
        active = queue.active().get("run")
        require(
            active is None or active["id"] != run_id,
            "Delivered run still holds the active claim.",
        )
        observed = collect_run(
            queue,
            github,
            run_id,
            scenario["project_id"],
            task["issue_number"],
            snapshot=state["snapshots"].get(run_id),
        )
        workspace = observed["delivery"]["workspace"]
        require(
            workspace["repository_id"] == scenario["repository_id"]
            and workspace["repository"] == scenario["repository"]
            and workspace["base_branch"] == scenario["base_branch"],
            "Acceptance repository/base changed.",
        )
        require(
            any(not name.endswith(".md") for name in observed["changed_files"]),
            "Acceptance requires an unmet source-level change, not documentation-only output.",
        )
        entries = evidence_entries(queue, run_id)
        for entry in entries:
            if entry["kind"].startswith("native_"):
                validate_native_artifact(entry["kind"], entry["data"])
        artifacts = factory_artifacts(queue, run_id)
        documents = factory_documents(queue, run, artifacts)
        chain = check_factory_chain(
            artifacts, entries, run, observed, scenario, documents
        )
        verify_native_receipts(entries, worker)
        merged = observe_human_merge(github, observed)
        checks = verify_checks(github, observed, scenario)
        frames = []
        if "ui" in task.get("surfaces", []):
            frames = verify_browser(
                observed,
                chain,
                (browser_evidence or {}).get(run_id),
                worker,
                capabilities,
            )
        record = {
            **observed,
            "factory": chain,
            "human_merge": merged,
            "claim_released": True,
            "required_ci": checks,
            "screenshots": frames,
        }
        state["snapshots"][run_id] = observed
        records.append(record)
        if merged["status"] != "human_merged":
            require(
                index == 1 or state["runs"][1] is None,
                "Dependent B started before A's verified human merge.",
            )
            break
    if len(records) == 2:
        require(
            len({entry["run_id"] for entry in records}) == 2,
            "Acceptance reused a run identity.",
        )
        require(
            len(
                {
                    entry["delivery"]["delivery"]["pull_request"]["number"]
                    for entry in records
                }
            )
            == 2,
            "Acceptance requires one distinct PR for each issue.",
        )
        first, second = records
        require(
            timestamp(second["claimed_at"])
            >= timestamp(first["human_merge"]["merged_at"]),
            "B was claimed before the human merge of A.",
        )
        base = second["delivery"]["workspace"]["base_revision"]
        merge = first["human_merge"]["merge_sha"]
        comparison = github.request(
            "GET", f"/repos/{scenario['repository']}/compare/{merge}...{base}"
        )
        require(
            comparison.get("status") in {"ahead", "identical"}
            and comparison.get("merge_base_commit", {}).get("sha") == merge,
            "B's inspected base does not contain A's actual human merge.",
        )
    complete = len(records) == 2 and all(
        entry["human_merge"]["status"] == "human_merged" for entry in records
    )
    awaiting_merge = records and records[-1]["human_merge"]["status"] != "human_merged"
    stage = (
        "complete"
        if complete
        else "awaiting_human_merge" if awaiting_merge else "awaiting_delivery"
    )
    missing = []
    try:
        verify_stack(stack_report, capabilities, scenario["source_revision"])
    except (ValueError, TypeError, KeyError) as exc:
        missing.append(str(exc))
    missing.extend(verify_failures(failure_evidence or {}, worker))
    qualified = complete and not missing and state["evidence_kind"] == "live"
    return {
        "schema_version": 1,
        "kind": "factory-human-merge-acceptance",
        "operation_id": state["operation_id"],
        "scenario_digest": state["scenario_digest"],
        "evidence_kind": state["evidence_kind"],
        "stage": stage,
        "qualified": qualified,
        "records": records,
        "missing_prerequisites": missing,
        "merge_writes": 0,
        "auto_merge_writes": 0,
        "release_status": "qualified" if qualified else "not-qualified",
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "observe", "collect"))
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--scenario", type=Path)
    parser.add_argument(
        "--evidence-kind", choices=("fixture", "live"), default="fixture"
    )
    parser.add_argument("--run-a")
    parser.add_argument("--run-b")
    parser.add_argument("--stack-report", type=Path)
    parser.add_argument("--failure-evidence", type=Path)
    parser.add_argument("--browser-evidence", type=Path)
    parser.add_argument("--github-token-file", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "prepare":
            require(
                args.scenario is not None, "Prepare requires a private scenario file."
            )
            result = prepare(
                args.state,
                private_json(args.scenario),
                evidence_kind=args.evidence_kind,
            )
        else:
            from .queue_acceptance import client
            from vibearr_executor.client import Client as ExecutorClient

            require(
                args.github_token_file is not None,
                "Select a private GitHub read credential file.",
            )
            material = private_json(args.github_token_file)
            github = MergeGitHub(material["secret"], read_only=True)
            queue = client()
            with State(args.state) as storage:
                state = storage.load()
                if args.command == "observe":
                    observe(queue, state, run_a=args.run_a, run_b=args.run_b)
                    storage.save(state)
                stack = private_json(args.stack_report) if args.stack_report else {}
                failures = (
                    private_json(args.failure_evidence) if args.failure_evidence else {}
                )
                browser = (
                    private_json(args.browser_evidence) if args.browser_evidence else {}
                )
                worker = ExecutorClient.from_environment()
                result = collect(
                    queue,
                    github,
                    state,
                    stack_report=stack,
                    capabilities=worker.capabilities(),
                    failure_evidence=failures,
                    browser_evidence=browser,
                    worker=worker,
                )
                state["stage"], state["report"] = result["stage"], result
                storage.save(state)
    except (
        APIError,
        DeliveryError,
        MergeWait,
        MergeRepair,
        OSError,
        ValueError,
        RuntimeError,
        KeyError,
        TypeError,
    ) as exc:
        print(
            json.dumps(
                {
                    "qualified": False,
                    "error": type(exc).__name__,
                    "reason": "Preserve this acceptance state; reconcile the original evidence without dispatching work.",
                }
            )
        )
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


def observe(queue, state, *, run_a=None, run_b=None):
    queue = ReadOnlyQueue(queue)
    runs = list(state["runs"])
    for index, run_id in enumerate((run_a, run_b)):
        if run_id is None:
            continue
        identifier(run_id)
        require(
            runs[index] in {None, run_id},
            "Resume the original acceptance run; its identity cannot be replaced.",
        )
        response = queue.reconcile(run_id)
        run, item = response["run"], response["item"]
        scenario, task = state["scenario"], state["scenario"]["tasks"][index]
        require(
            run["id"] == run_id
            and item["project"]["id"] == scenario["project_id"]
            and item["issue_number"] == task["issue_number"]
            and item["github_repository_id"] == scenario["repository_id"]
            and run["configuration_revision"] == scenario["configuration_revision"],
            "Observed run differs from the frozen project, issue, repository or configuration.",
        )
        runs[index] = run_id
    require(runs[1] is None or runs[0] is not None, "Observe A before its dependent B.")
    require(
        runs[0] is None or runs[0] != runs[1],
        "Acceptance tasks must use distinct runs.",
    )
    state["runs"] = runs


if __name__ == "__main__":
    raise SystemExit(main())
