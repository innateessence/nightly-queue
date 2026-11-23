"""Read-only verification of pinned factory failure scenarios."""

from copy import deepcopy
import math
import os
from pathlib import Path
import socket
import time

from .bootstrap_files import private_json
from .native_contract import (
    canonical,
    digest,
    identifier,
    receipt_summary,
    validate_native_artifact,
)
from .self_acceptance import evidence_entries, timestamp


FAILURE_CASES = frozenset(
    {
        "failed-check-repair",
        "review-request-repair",
        "repair-exhaustion",
        "missing-capability",
        "cancellation",
        "controller-restart",
        "worker-restart",
        "independent-work-during-human-review",
    }
)
BINDING = (
    "run_id",
    "context_id",
    "context_digest",
    "specification_id",
    "specification_digest",
    "source_digest",
    "tree_sha",
    "base_sha",
    "head_sha",
    "configuration_digest",
    "round",
)


class IncompleteEvidence(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise ValueError(message)


def need(condition, message):
    if not condition:
        raise IncompleteEvidence(message)


def finite(value):
    require(
        type(value) in {int, float} and math.isfinite(value) and value >= 0,
        "Scenario accounting must be finite and nonnegative.",
    )
    return value


def controller_witness(runner_id):
    found = []
    for process in Path("/proc").iterdir():
        if not process.name.isdigit() or int(process.name) == os.getpid():
            continue
        try:
            arguments = (process / "cmdline").read_bytes().split(b"\0")
            if b"vibearr_runner" not in arguments:
                continue
            status = dict(
                line.split(":", 1)
                for line in (process / "status").read_text().splitlines()
                if ":" in line
            )
            fields = (process / "stat").read_text().rsplit(") ", 1)[1].split()
            selected_environment = {}
            for field in (process / "environ").read_bytes().split(b"\0"):
                key, _, value = field.partition(b"=")
                if key in {b"VIBEARR_RUNNER_STATE_DIR", b"NQ_RUNNER_STATE_DIR"}:
                    selected_environment[key.decode()] = value.decode()
        except FileNotFoundError:
            continue
        require(
            int(status["Uid"].split()[0]) == 10001,
            "Controller witness has unexpected authority.",
        )
        directory = selected_environment.get("VIBEARR_RUNNER_STATE_DIR")
        directory = directory or selected_environment.get(
            "NQ_RUNNER_STATE_DIR", "/var/lib/coding-runner"
        )
        found.append(
            {
                "kind": "actual-controller-process",
                "runner_id": runner_id,
                "pid": int(process.name),
                "start_ticks": int(fields[19]),
                "uid": 10001,
                "journal_path": str(Path(directory) / "journal.json"),
            }
        )
    require(len(found) == 1, "Expected one actual trusted controller process.")
    return found[0]


def capture_checkpoint(journal_path, *, worker, operation_id):
    """Capture once inside the trusted runner; never acquire its lock or dispatch work."""
    identifier(operation_id)
    path = Path(journal_path)
    before = private_json(path)
    native = before.get("native", {})
    run_id = identifier(native.get("run_id"))
    context = native.get("context", {})
    recovery = native.get("factory", {}).get("recovery", {})
    operations = recovery.get("operations", {})
    require(len(operations) <= 1000, "Checkpoint exceeds bounded operation history.")
    statuses = {key: worker.status(identifier(key)) for key in operations}
    capabilities = worker.capabilities()
    witness = controller_witness(before.get("runner_id"))
    require(
        Path(witness["journal_path"]).samefile(path),
        "Observe the actual controller's registered journal.",
    )
    require(
        before == private_json(path),
        "Journal changed during observation; capture again.",
    )
    value = {
        "schema_version": 1,
        "kind": "factory-observation",
        "operation_id": operation_id,
        "observed_at": time.time(),
        "runner_id": before.get("runner_id"),
        "run_id": run_id,
        "container": socket.gethostname(),
        "context_id": context.get("id"),
        "context_digest": context.get("digest"),
        "controller": witness,
        "native": deepcopy(native),
        "budget": deepcopy(before.get("budgets", {}).get(run_id)),
        "capabilities": capabilities,
        "statuses": statuses,
    }
    require(len(canonical(value)) <= 8 * 1024**2, "Checkpoint exceeds its bound.")
    return value


def authenticate_checkpoint(
    value, worker, *, journal_path="/var/lib/vibearr/journal.json"
):
    require(
        abs(time.time() - finite(value.get("observed_at"))) <= 30,
        "New checkpoint must be authenticated while its observation is current.",
    )
    current = capture_checkpoint(
        journal_path, worker=worker, operation_id=value.get("operation_id")
    )
    require(
        digest({key: content for key, content in value.items() if key != "observed_at"})
        == digest(
            {key: content for key, content in current.items() if key != "observed_at"}
        ),
        "Checkpoint differs from the actual trusted journal, process or worker.",
    )
    return digest(value)


def verify_operation_manifest(worker, manifest, *, status=None):
    operation = identifier(manifest.get("operation_id"))
    status = worker.status(operation) if status is None else status
    receipt = receipt_summary(status)
    protected = worker.termination_receipt(operation)
    require(
        type(protected.get("schema_version")) is int
        and protected["schema_version"] == 1
        and protected.get("operation_id") == operation
        and protected.get("run_id") == manifest["run_id"] == receipt["run_id"]
        and protected.get("instance_id") == receipt["execution_identity"]["instance_id"]
        and protected.get("state") == "exited"
        and protected.get("resources_terminated") is True
        and protected.get("manifest_digest") == digest(manifest),
        "Scenario manifest differs from the authenticated original worker manifest.",
    )
    sealed = digest(
        {
            "manifest": manifest,
            "result": status["result"],
            "boot": status["worker_boot"],
        }
    )
    require(
        protected.get("receipt_digest") == status.get("receipt_digest") == sealed,
        "Scenario sealed worker receipt differs from its original manifest, result or boot.",
    )


def authenticate_manifest(worker, manifest, receipt):
    status = worker.status(identifier(manifest.get("operation_id")))
    require(
        receipt_summary(status) == receipt,
        "Scenario manifest result differs from protected termination.",
    )
    verify_operation_manifest(worker, manifest, status=status)


def validate_validation_binding(intent, validation, receipts, *, worker=None):
    require(
        isinstance(intent, dict) and intent.get("kind") == "validation",
        "Scenario validation lacks its original immutable intent.",
    )
    validate_native_artifact("native_intent", intent)
    validate_native_artifact("native_validation", validation)
    session = intent["data"]
    require(
        all(
            session[key] == validation[key]
            for key in ("run_id", "source_digest", "tree_sha", "profile")
        ),
        "Scenario validation changed its original run, profile or source.",
    )
    for check, original in zip(validation["checks"], session["operations"]):
        receipt = check["receipt"]
        manifest = original["manifest"]
        require(
            check["id"] == original["id"]
            and receipts.get(receipt["operation_id"]) == receipt,
            "Scenario validation receipt lacks authenticated original termination.",
        )
        require(
            manifest["operation_id"] == receipt["operation_id"]
            and manifest["run_id"] == validation["run_id"] == receipt["run_id"]
            and manifest["source_digest"]
            == receipt["source_digest"]
            == validation["source_digest"],
            "Scenario validation receipt differs from original source or operation.",
        )
        if worker is not None:
            authenticate_manifest(worker, manifest, receipt)


class History:
    def __init__(self, pin, *, queue, worker, scenario):
        from .factory_acceptance import factory_artifacts, immutable_document

        self.pin, self.worker, self.scenario = pin, worker, scenario
        self.run_id = identifier(pin.get("run_id"))
        response = queue.reconcile(self.run_id)
        self.run, self.item = response["run"], response["item"]
        run, item = self.run, self.item
        require(
            run["id"] == self.run_id
            and item["project"]["id"] == scenario["project_id"]
            and item["github_repository_id"] == scenario["repository_id"],
            "Scenario run scope differs.",
        )
        require(
            run["configuration_revision"] == scenario["configuration_revision"],
            "Scenario configuration differs.",
        )
        snapshot = run["configuration_snapshot"]
        require(
            snapshot.get("execution_kind") == "native"
            and snapshot.get("autonomous") is True
            and snapshot["execution_policy"]["delivery_mode"] == "pull_request"
            and snapshot["execution_policy"]["repair_limit"]
            == scenario["repair_limit"],
            "Scenario native delivery or repair policy differs.",
        )
        require(
            run["source_snapshot"].get("issue_number") == item["issue_number"]
            and run["source_snapshot"].get("repository_id")
            == scenario["repository_id"],
            "Scenario frozen issue source differs.",
        )
        self.context = immutable_document(
            queue, self.run_id, "contexts", identifier(pin.get("context_id"))
        )
        require(
            self.context["digest"] == pin.get("context_digest")
            and self.context["document"]["run_id"] == self.run_id
            and run["context_reference"] == self.context["id"],
            "Scenario immutable context differs.",
        )
        account = self.context["document"]["account"]
        require(
            account["backend"] == scenario["provider"]
            and account["credential_id"] == scenario["connection_id"],
            "Scenario selected account differs.",
        )
        source = self.context["document"]["source"]
        frozen = run["source_snapshot"]
        require(
            all(
                source.get(key) == frozen.get(key)
                for key in ("title", "body", "comments")
            ),
            "Scenario context changed frozen issue source.",
        )
        repository = self.context["document"]["repository"]
        require(
            all(
                repository.get(key) == scenario[key]
                for key in ("repository_id", "repository", "base_branch")
            )
            and repository.get("branch") == item["branch_name"],
            "Scenario context repository or branch differs.",
        )
        self.entries = evidence_entries(queue, self.run_id)
        self.artifacts = factory_artifacts(queue, self.run_id)
        for name, actual in (
            ("evidence_ids", [entry["operation_id"] for entry in self.entries]),
            ("artifact_ids", [entry["id"] for entry in self.artifacts]),
        ):
            require(
                isinstance(pin.get(name), list)
                and len(pin[name]) == len(set(pin[name]))
                and set(pin[name]) == set(actual),
                "Pinned scenario evidence history differs: " + name,
            )
        self.facts = {}
        for entry in self.entries:
            if entry["kind"].startswith("native_"):
                validate_native_artifact(entry["kind"], entry["data"])
            self.facts.setdefault(entry["kind"], []).append(entry)
        self.intents = {
            entry["operation_id"]: entry["data"]
            for entry in self.facts.get("native_intent", [])
        }
        self.receipts = {}
        for entry in self.facts.get("native_termination", []):
            for receipt in entry["data"]["operations"]:
                operation = receipt["operation_id"]
                require(
                    receipt["run_id"] == self.run_id,
                    "Termination belongs to another scenario run.",
                )
                require(
                    operation not in self.receipts
                    or self.receipts[operation] == receipt,
                    "Scenario termination changed.",
                )
                self.receipts[operation] = receipt
        for operation, receipt in self.receipts.items():
            require(
                receipt_summary(worker.status(operation)) == receipt,
                "Scenario receipt differs from authenticated original operation.",
            )
            require(
                receipt.get("schema_version") == 2
                and receipt["execution_identity"]["runtime"] == "compact-worker-v1",
                "Scenario requires compact worker receipts.",
            )
        for entry in self.facts.get("native_agent", []):
            result = entry["data"]
            intent = self.intents.get(result["intent_id"])
            require(
                intent is not None
                and intent["kind"] == "agent"
                and intent["data"]["session"]["manifest"]["operation_id"]
                == result["receipt"]["operation_id"]
                and self.receipts.get(result["receipt"]["operation_id"])
                == result["receipt"],
                "Scenario has an orphan or substituted native agent result.",
            )
        for entry in self.facts.get("native_validation", []):
            self.validation(entry["data"])
        self.rounds = {}
        for artifact in self.artifacts:
            document = artifact["document"]
            require(
                document["configuration_digest"] == digest(snapshot)
                and document["context_id"] == self.context["id"]
                and document["context_digest"] == self.context["digest"],
                "Scenario artifact binding differs.",
            )
            current = self.rounds.setdefault(document["round"], {})
            require(
                document["role"] not in current, "Scenario repeats a role in one round."
            )
            current[document["role"]] = artifact
            if "execution" in document:
                self.execution(document)
        self.attempts = sorted(
            {
                intent["attempt"]
                for intent in self.intents.values()
                if intent["kind"] == "agent"
            }
        )
        limit = scenario["repair_limit"]
        require(
            not self.attempts
            or self.attempts == list(range(max(self.attempts) + 1))
            and max(self.attempts) <= limit,
            "Scenario reset or exceeded shared repair attempts.",
        )
        if self.rounds:
            require(
                sorted(self.rounds) == list(range(1, max(self.rounds) + 1))
                and max(self.rounds) <= limit + 1,
                "Scenario reset or exceeded factory rounds.",
            )
        self.accounting()

    def validation(self, value):
        intent = self.intents.get(value["intent_id"])
        validate_validation_binding(intent, value, self.receipts, worker=self.worker)
        require(
            intent["context_id"] == self.context["id"]
            and intent["context_digest"] == self.context["digest"]
            and intent["policy_digest"] == self.context["document"]["policy_digest"]
            and value["run_id"] == self.run_id,
            "Scenario validation changed its original context, policy or source.",
        )

    def execution(self, document):
        execution = document["execution"]
        intent = self.intents.get(execution["intent_id"])
        results = [
            entry["data"]
            for entry in self.facts.get("native_agent", [])
            if entry["operation_id"] == execution["result_id"]
            or str(entry["id"]) == execution["result_id"]
        ]
        require(
            intent is not None and len(results) == 1,
            "Scenario role lacks immutable native intent/result.",
        )
        session = intent["data"]["session"]
        result = results[0]
        account = self.context["document"]["account"]
        expected_account = {
            **account,
            "model": self.run["configuration_snapshot"]["effective"]["factory"][
                "roles"
            ][document["role"]]["model"],
        }
        expected = {
            key: document[key]
            for key in ("role", "round", "specification_id", "specification_digest")
        }
        expected.update(
            {key: execution[key] for key in ("input_digest", "prompt_digest")}
        )
        require(
            intent["context_id"] == document["context_id"]
            and intent["context_digest"] == document["context_digest"]
            and intent["attempt"] == document["round"] - 1
            and intent["data"].get("factory") == expected,
            "Scenario dispatch changed context, specification or shared attempt.",
        )
        require(
            session["adapter"] == self.scenario["provider"]
            and session["agent_version"] == self.scenario["agent_version"]
            and session["adapter"] == execution["adapter"]
            and session["account"]["model"] == execution["model"]
            and session["account"] == expected_account,
            "Scenario role account or provider differs.",
        )
        manifest = session["manifest"]
        operation = execution["operation_id"]
        receipt = self.receipts.get(operation)
        require(
            receipt is not None
            and result["receipt"] == receipt
            and result["successful"] is True
            and result["intent_id"] == execution["intent_id"]
            and result["session_id"] == execution["session_id"]
            and result["agent_version"] == execution["agent_version"],
            "Scenario role lacks genuine completed execution.",
        )
        allowance = finite(
            self.run["configuration_snapshot"]["global"]["task_timeout_seconds"]
        )
        require(
            receipt["ended_at"]
            <= timestamp(self.run["created_at"]).timestamp() + allowance,
            "Successful scenario role exceeded the frozen overall deadline.",
        )
        require(
            manifest["run_id"] == self.run_id
            and manifest["operation_id"] == operation
            and manifest["source_digest"] == execution["input_source_digest"]
            and manifest["context_digest"] == execution["input_digest"]
            and session["execution_identity"]
            == execution["execution_identity"]
            == receipt["execution_identity"]
            and receipt["started_at"] == execution["started_at"]
            and receipt["ended_at"] == execution["ended_at"],
            "Scenario role source, input, worker or time differs.",
        )
        require(
            receipt["source_digest"] == manifest["source_digest"],
            "Scenario role inspected another source.",
        )
        if document["role"] == "build":
            imports = [
                entry["data"]
                for entry in self.facts.get("native_workspace", [])
                if entry["data"]["agent_intent_id"] == execution["intent_id"]
            ]
            require(
                len(imports) == 1, "Scenario build lacks its exact imported workspace."
            )
            imported = imports[0]
            workspace = imported["workspace"]
            require(
                imported["context_id"] == document["context_id"]
                and imported["context_digest"] == document["context_digest"]
                and imported["source_digest"] == document["source_digest"]
                and imported["export_digest"] == receipt["export_digest"]
                and workspace["tree_sha"] == document["tree_sha"]
                and workspace["commit_sha"] == document["head_sha"]
                and workspace["base_revision"] == document["base_sha"],
                "Scenario build imported another source or tree.",
            )
        else:
            require(
                receipt["source_digest"]
                == receipt["export_digest"]
                == document["source_digest"],
                "Scenario read-only role changed its inspected source.",
            )

    def accounting(self):
        agents = [
            intent for intent in self.intents.values() if intent["kind"] == "agent"
        ]
        elapsed = 0.0
        controls = self.run["configuration_snapshot"]["global"]
        overall = timestamp(self.run["created_at"]).timestamp() + finite(
            controls["task_timeout_seconds"]
        )
        for intent in agents:
            session = intent["data"]["session"]
            operation = session["manifest"]["operation_id"]
            require(
                timestamp(session["manifest"]["deadline"]).timestamp() <= overall,
                "Scenario dispatch extended the frozen overall deadline.",
            )
            role = intent["data"].get("factory", {}).get("role")
            expected_account = {**self.context["document"]["account"]}
            if role:
                expected_account["model"] = self.run["configuration_snapshot"][
                    "effective"
                ]["factory"]["roles"][role]["model"]
            require(
                intent["context_id"] == self.context["id"]
                and intent["context_digest"] == self.context["digest"]
                and intent["policy_digest"] == self.context["document"]["policy_digest"]
                and session["account"] == expected_account
                and session["manifest"]["run_id"] == self.run_id,
                "Scenario original dispatch changed frozen context, account or run.",
            )
            receipt = self.receipts.get(operation)
            need(
                receipt is not None,
                "Original scenario operation termination remains unknown.",
            )
            authenticate_manifest(self.worker, session["manifest"], receipt)
            require(
                receipt["execution_identity"] == session["execution_identity"],
                "Scenario worker identity changed.",
            )
            start, end = receipt["started_at"], receipt["ended_at"]
            if start is None:
                status = self.worker.status(operation)
                restarted = status.get("result", {}).get("worker_restart") is True
                require(
                    receipt["canceled"] is True or restarted,
                    "Unstarted scenario operation lacks cancellation or restart proof.",
                )
                continue
            finished = self.run.get("finished_at")
            require(
                end is not None
                and timestamp(self.run["created_at"]).timestamp() <= start <= end
                and (finished is None or end <= timestamp(finished).timestamp()),
                "Scenario role timing differs from its run.",
            )
            elapsed += finite(end - start)
        require(
            elapsed <= finite(controls["implementation_timeout_seconds"]),
            "Scenario reset aggregate role budget.",
        )

    def terminal_failure(self, outcome="failed"):
        need(self.run["state"] == "completed", "Scenario run has not completed.")
        require(
            self.run["native_outcome"] == outcome
            and self.run["result_status"] == "failed",
            "Scenario terminal outcome differs.",
        )
        require(
            not self.run["result_url"]
            and not self.facts.get("native_delivery")
            and not self.facts.get("verified_delivery")
            and all(
                intent["kind"] not in {"delivery", "merge", "revision"}
                for intent in self.intents.values()
            ),
            "Failed scenario attempted publication.",
        )

    def failure(self, round_number, reason):
        roles = self.rounds.get(round_number, {})
        need(
            "build" in roles and "checks" in roles,
            "Scenario lacks completed build/check failure evidence.",
        )
        checks = roles["checks"]
        build = roles["build"]
        require(
            all(checks["document"][key] == build["document"][key] for key in BINDING),
            "Failed checks inspected another candidate.",
        )
        reference = checks["document"]["payload"]["validation_id"]
        validations = [
            entry
            for entry in self.facts.get("native_validation", [])
            if entry["operation_id"] == reference or str(entry["id"]) == reference
        ]
        require(
            len(validations) == 1, "Scenario checks lack protected validation result."
        )
        validation = validations[0]["data"]
        document = checks["document"]
        intent = self.intents.get(validation["intent_id"])
        require(
            intent is not None
            and intent["kind"] == "validation"
            and intent["attempt"] == round_number - 1
            and intent["context_id"] == document["context_id"]
            and intent["context_digest"] == document["context_digest"]
            and intent["data"]["profile"] == validation["profile"],
            "Scenario checks lack their original bound validation intent.",
        )
        require(
            validation["run_id"] == self.run_id
            and validation["source_digest"] == document["source_digest"]
            and validation["tree_sha"] == document["tree_sha"]
            and validation["state"] == document["payload"]["state"],
            "Scenario validation source or outcome differs.",
        )
        for check in validation["checks"]:
            receipt = check["receipt"]
            require(
                self.receipts.get(receipt["operation_id"]) == receipt,
                "Validation operation lacks confirmed original termination.",
            )
            original = next(
                entry["manifest"]
                for entry in intent["data"]["operations"]
                if entry["id"] == check["id"]
            )
            require(
                original["operation_id"] == receipt["operation_id"]
                and original["run_id"] == self.run_id
                and original["source_digest"]
                == receipt["source_digest"]
                == document["source_digest"]
                and intent["data"]["source_digest"] == document["source_digest"]
                and intent["data"]["tree_sha"] == document["tree_sha"],
                "Scenario validation receipt inspected another source or operation.",
            )
        if reason == "checks":
            require(
                validation["state"] == "failed"
                and any(
                    type(check["receipt"]["exit_code"]) is int
                    and check["receipt"]["exit_code"] != 0
                    for check in validation["checks"]
                ),
                "Scenario requires actual failed checks.",
            )
            return checks["sequence"]
        need("routing" in roles, "Scenario lacks required review routing.")
        from .delivery import DeliveryError
        from .factory_delivery import validate_routing

        try:
            validate_routing(
                self.run["configuration_snapshot"],
                self.artifacts,
                round_number,
                self.context["id"],
                [{**entry, "payload": entry["data"]} for entry in self.entries],
            )
        except DeliveryError as error:
            raise ValueError(str(error)) from None
        required = roles["routing"]["document"]["payload"]["required_reviewers"]
        require(
            validation["state"] == "passed",
            "Review repair requires passing protected checks.",
        )
        need(set(required) <= set(roles), "Scenario lacks completed required reviews.")
        reviews = [roles[role] for role in required]
        for review in reviews:
            require(
                all(review["document"][key] == document[key] for key in BINDING),
                "Repair review inspected another candidate.",
            )
        require(
            any(
                review["document"]["payload"]["verdict"] == "changes"
                for review in reviews
            )
            and all(
                review["document"]["payload"]["verdict"] in {"pass", "changes"}
                for review in reviews
            ),
            "Scenario requires completed reviewer-requested changes.",
        )
        return max(review["sequence"] for review in reviews)

    def ready(self, queue, github):
        from .factory_acceptance import immutable_document
        from .factory_delivery import checked_factory_workspace
        from .native_contract import validation_evidence
        from .native_evidence import validate_native_evidence
        from .self_acceptance import pulls_for_branch

        workspaces = self.facts.get("native_workspace", [])
        need(bool(workspaces), "Repaired scenario lacks inspected workspace.")
        workspace = workspaces[-1]["data"]["workspace"]
        plan = immutable_document(
            queue, self.run_id, "plans", self.run["plan_reference"]
        )
        authority = checked_factory_workspace(
            self.run["configuration_snapshot"],
            plan,
            self.context,
            workspace,
            self.artifacts,
            [{**entry, "payload": entry["data"]} for entry in self.entries],
            self.run_id,
        )
        require(isinstance(authority, dict), "Scenario factory gate is disabled.")
        need(self.run["state"] == "completed", "Repaired scenario is still active.")
        require(
            self.run["native_outcome"] == "delivered"
            and self.run["result_status"] == "succeeded",
            "Repair did not finish with truthful delivery.",
        )
        saved = self.facts.get("verified_delivery", [])
        publications = self.facts.get("native_delivery", [])
        need(
            len(saved) == len(publications) == 1,
            "Scenario lacks one immutable verified publication.",
        )
        delivery = validate_native_evidence(saved[0]["data"])
        publication = publications[0]["data"]
        require(
            delivery["run_id"] == self.run_id
            and delivery["runner_id"] == self.run["runner_id"]
            and delivery["context"]
            == {"id": self.context["id"], "digest": self.context["digest"]}
            and delivery["configuration_revision"]
            == self.scenario["configuration_revision"]
            and delivery["policy_digest"] == self.context["document"]["policy_digest"]
            and delivery["workspace"] == publication["workspace"] == workspace
            and delivery["delivery"]["pull_request"] == publication["pull_request"]
            and delivery["delivery"]["mode"] == "pull_request"
            and delivery["delivery"]["pushed_sha"]
            == publication["pushed_sha"]
            == workspace["commit_sha"]
            and self.run["result_url"] == publication["pull_request"]["url"],
            "Scenario publication differs from the verified candidate or run.",
        )
        build = self.rounds[max(self.rounds)]["build"]["document"]["execution"]
        session = self.intents[build["intent_id"]]["data"]["session"]
        require(
            delivery["runtime"]
            == {
                "image_digest": session["image_digest"],
                "profile_id": session["manifest"]["profile_id"],
                "agent_version": build["agent_version"],
                "session_id": build["session_id"],
            },
            "Scenario publication changed its original builder identity.",
        )
        validations = [
            entry["data"]
            for entry in self.facts.get("native_validation", [])
            if entry["data"]["state"] == "passed"
            and entry["data"]["tree_sha"] == workspace["tree_sha"]
        ]
        require(
            bool(validations)
            and validation_evidence(validations[-1]) == delivery["validation"],
            "Scenario publication omitted protected validation receipts.",
        )
        for intent_id in publication["intent_ids"]:
            intent = self.intents.get(intent_id)
            require(
                intent is not None
                and intent["kind"] == "delivery"
                and intent["data"]["publication_id"] == publication["publication_id"]
                and intent["data"]["workspace"] == workspace,
                "Scenario publication lacks its original immutable delivery intent.",
            )
        pulls = pulls_for_branch(
            github, self.scenario["repository"], workspace["branch"]
        )
        require(
            len(pulls) == 1
            and pulls[0]["number"] == publication["pull_request"]["number"]
            and pulls[0]["head"]["sha"] == workspace["commit_sha"]
            and pulls[0]["head"]["ref"] == workspace["branch"]
            and pulls[0]["base"]["ref"] == workspace["base_branch"]
            and (pulls[0]["state"] == "open" or pulls[0].get("merged_at")),
            "Scenario has missing, duplicate or closed-unmerged publication.",
        )


def checkpoint(value, history, operation_id):
    require(
        isinstance(value, dict)
        and type(value.get("schema_version")) is int
        and value["schema_version"] == 1
        and value.get("kind") == "factory-observation"
        and value.get("operation_id") == operation_id,
        "Checkpoint belongs to another acceptance operation.",
    )
    require(
        all(
            value.get(key) == history.pin[key]
            for key in ("run_id", "context_id", "context_digest")
        ),
        "Checkpoint belongs to another run/context.",
    )
    native = value["native"]
    controls = history.run["configuration_snapshot"]["global"]
    overall = timestamp(history.run["created_at"]).timestamp() + finite(
        controls["task_timeout_seconds"]
    )
    require(
        timestamp(native["deadline"]).timestamp() == overall,
        "Checkpoint changed the frozen overall deadline.",
    )
    require(
        native["run_id"] == history.run_id
        and all(
            native["context"].get(key) == history.context.get(key)
            for key in ("id", "digest", "document")
        )
        and value["runner_id"] == history.run["runner_id"],
        "Checkpoint changed immutable run or runner identity.",
    )
    require(
        timestamp(history.run["created_at"]).timestamp()
        <= finite(value["observed_at"]),
        "Checkpoint predates the run.",
    )
    require(
        type(native["attempt"]) is int
        and 0 <= native["attempt"] <= history.scenario["repair_limit"],
        "Checkpoint repair attempt differs.",
    )
    caps = value["capabilities"]
    require(
        caps.get("runtime") == "compact-worker-v1"
        and caps.get("binding_digest")
        == history.context["document"]["runtime_binding_digest"],
        "Checkpoint is not from the frozen compact worker binding.",
    )
    return native


def restart(history, operation_id, *, worker_restart):
    points = history.pin.get("checkpoints", [])
    need(
        len(points) == 2, "Restart requires before/after trusted journal observations."
    )
    before, after = points
    first, second = [checkpoint(value, history, operation_id) for value in points]
    require(
        before["observed_at"] < after["observed_at"],
        "Restart observation order differs.",
    )
    require(
        before["container"] == after["container"] and bool(before["container"]),
        "Restart observations belong to different controller containers.",
    )
    for key in ("run_id", "attempt", "deadline", "context", "plan"):
        require(
            first.get(key) == second.get(key),
            "Restart reset frozen identity, repair attempt or deadline.",
        )
    prior = first["factory"]["recovery"]
    current = second["factory"]["recovery"]
    require(
        prior["frozen"] == current["frozen"]
        and prior["repairs"] == current["repairs"]
        and set(prior["operations"]) == set(current["operations"]),
        "Restart replaced original operation or repair history.",
    )
    active = [
        key
        for key, record in prior["operations"].items()
        if record["state"] in {"running", "dispatching", "unknown"}
    ]
    need(bool(active), "Restart lacks an interrupted original factory operation.")
    for key, old in prior["operations"].items():
        new = current["operations"][key]
        for field in (
            "binding",
            "session",
            "requested_deadline",
            "deadline",
            "dispatched_at",
        ):
            require(
                old.get(field) == new.get(field),
                "Restart changed original dispatch binding.",
            )
        require(
            finite(new["elapsed"]) >= finite(old["elapsed"]),
            "Restart reset spent operation time.",
        )
    old_budget, new_budget = before["budget"], after["budget"]
    require(
        isinstance(old_budget, dict)
        and isinstance(new_budget, dict)
        and old_budget["limits"] == new_budget["limits"]
        and finite(new_budget["total"]) >= finite(old_budget["total"]),
        "Restart reset cumulative budget.",
    )
    for stage, elapsed in old_budget["stages"].items():
        require(
            finite(new_budget["stages"].get(stage, -1)) >= finite(elapsed),
            "Restart reset spent stage budget.",
        )
    for key, charge in old_budget.get("charges", {}).items():
        later = new_budget.get("charges", {}).get(key, {})
        require(
            later.get("stage") == charge["stage"]
            and finite(later.get("elapsed", -1)) >= finite(charge["elapsed"]),
            "Restart lost an original operation charge.",
        )
    old_caps, new_caps = before["capabilities"], after["capabilities"]
    require(
        old_caps["instance_id"] == new_caps["instance_id"]
        and old_caps["binding_digest"] == new_caps["binding_digest"],
        "Restart replaced worker volume or runtime binding.",
    )
    if not worker_restart:
        old, new = before["controller"], after["controller"]
        require(
            old["kind"] == new["kind"] == "actual-controller-process"
            and old["uid"] == new["uid"] == 10001
            and old["runner_id"] == new["runner_id"] == history.run["runner_id"]
            and old["start_ticks"] != new["start_ticks"],
            "No actual controller process restart was observed.",
        )
        require(
            old_caps["worker_boot"] == new_caps["worker_boot"],
            "Controller restart changed the worker boot.",
        )
    else:
        require(
            old_caps["worker_boot"] != new_caps["worker_boot"],
            "Worker restart did not change boot.",
        )
    for key in active:
        old, new = before["statuses"][key], after["statuses"][key]
        require(
            old["operation_id"] == new["operation_id"] == key
            and old["state"] == "running",
            "Restart original operation was not actually running.",
        )
        actual = history.worker.status(key)
        require(
            new == actual and receipt_summary(new)["run_id"] == history.run_id,
            "Restart result differs from authenticated original status.",
        )
        require(
            new["execution_identity"] == old["execution_identity"],
            "Restart relabelled original execution boot.",
        )
        result = new["result"]
        if worker_restart:
            require(
                result.get("worker_restart") is True
                and result.get("reconciled_boot") == new_caps["worker_boot"]
                and result.get("exit_code") is None
                and not result.get("artifact"),
                "Worker restart lacks truthful original-operation reconciliation.",
            )
        require(
            current["operations"][key]["state"] == "exited"
            and current["operations"][key]["receipt_digest"]
            == receipt_summary(new)["receipt_digest"],
            "Restart journal lacks confirmed original receipt.",
        )


def verify_case(name, history, *, queue, github, records, operation_id):
    if name in {"failed-check-repair", "review-request-repair"}:
        reason = "checks" if name == "failed-check-repair" else "reviews"
        need(
            len(history.rounds) >= 2, "Repair requires a later completed factory round."
        )
        trigger = history.failure(1, reason)
        require(
            history.rounds[2]["build"]["sequence"] > trigger and 1 in history.attempts,
            "Repair build precedes failure or reused its attempt.",
        )
        first = history.rounds[1]
        if reason == "checks":
            reference = first["checks"]["document"]["payload"]["validation_id"]
            validation = next(
                entry["data"]
                for entry in history.facts["native_validation"]
                if entry["operation_id"] == reference
            )
            ended = max(check["receipt"]["ended_at"] for check in validation["checks"])
        else:
            required = first["routing"]["document"]["payload"]["required_reviewers"]
            ended = max(
                first[role]["document"]["execution"]["ended_at"] for role in required
            )
        require(
            ended <= history.rounds[2]["build"]["document"]["execution"]["started_at"],
            "Repair dispatched before the original failure completed.",
        )
        history.ready(queue, github)
        return
    if name == "repair-exhaustion":
        history.terminal_failure()
        limit = history.scenario["repair_limit"]
        require(
            history.attempts == list(range(limit + 1)),
            "Exhaustion did not spend the frozen repair allowance.",
        )
        for number in range(1, limit + 2):
            roles = history.rounds.get(number, {})
            checks = roles.get("checks", {}).get("document", {}).get("payload", {})
            history.failure(
                number, "checks" if checks.get("state") == "failed" else "reviews"
            )
        require(
            "exhaust" in history.run["error_summary"].lower(),
            "Terminal failure did not report exhaustion.",
        )
    elif name == "cancellation":
        history.terminal_failure("canceled")
        run = history.run
        require(
            run.get("cancellation_requested_at")
            and run.get("cancellation_confirmed_at"),
            "Scenario lacks protected cancellation request/confirmation.",
        )
        requested = timestamp(run["cancellation_requested_at"])
        confirmed = timestamp(run["cancellation_confirmed_at"])
        require(
            requested <= confirmed <= timestamp(run["finished_at"]),
            "Cancellation confirmation timing differs.",
        )
        need(
            bool(history.receipts), "Cancellation lacks original operation termination."
        )
        require(
            all(
                receipt["started_at"] is None
                or receipt["started_at"] <= requested.timestamp()
                for receipt in history.receipts.values()
            ),
            "Cancellation dispatched another operation after its request.",
        )
        require(
            all(
                receipt["ended_at"] is not None
                and receipt["ended_at"] <= confirmed.timestamp()
                for receipt in history.receipts.values()
            ),
            "Cancellation confirmed before all original operations terminated.",
        )
        require(
            any(
                receipt["canceled"] is True
                and receipt["ended_at"] is not None
                and requested.timestamp()
                <= receipt["ended_at"]
                <= confirmed.timestamp()
                for receipt in history.receipts.values()
            ),
            "Cancellation lacks protected canceled result.",
        )
    elif name == "missing-capability":
        from .factory_routing import require_route_capabilities, route
        from .native_sessions import NativeSessionError

        history.terminal_failure()
        points = history.pin.get("checkpoints", [])
        need(
            len(points) == 1,
            "Missing capability requires its trusted capability observation.",
        )
        point = points[0]
        checkpoint(point, history, operation_id)
        require(
            point["capabilities"].get("qualified") is True
            and point["capabilities"].get("protocol_version") == 2
            and point["capabilities"].get("qualification_version") == 1,
            "Missing reviewer capability requires an otherwise qualified worker.",
        )
        workspaces = history.facts.get("native_workspace", [])
        intakes = [
            roles["intake"] for roles in history.rounds.values() if "intake" in roles
        ]
        need(
            bool(workspaces) and bool(intakes),
            "Missing capability requires saved workspace paths and intake.",
        )
        source = workspaces[-1]["data"]
        intake = intakes[-1]["document"]["payload"]
        require(
            source["context_id"] == history.context["id"]
            and source["context_digest"] == history.context["digest"]
            and intake["verdict"] == "accept",
            "Missing capability routing source differs.",
        )
        routing = route(
            history.run["configuration_snapshot"]["effective"]["factory"],
            intake,
            source["factory_paths"],
        )
        require(
            not routing["protected_paths"],
            "Protected paths failed before capability routing.",
        )
        try:
            require_route_capabilities(routing, point["capabilities"])
        except NativeSessionError as error:
            require(
                str(error) in history.run["error_summary"],
                "Failure did not report the actual missing capability.",
            )
        else:
            raise ValueError("Scenario required capabilities were available.")
        required = set(routing["required_reviewers"])
        forbidden = required | (
            {"browser"} if {"reviewer-ui", "reviewer-ux"} & required else set()
        )
        require(
            not any(
                intent["kind"] == "agent"
                and intent["data"].get("factory", {}).get("role") in forbidden
                for intent in history.intents.values()
            ),
            "Missing capability dispatched an unauthorized reviewer.",
        )
        require(
            point["observed_at"] <= timestamp(history.run["finished_at"]).timestamp(),
            "Capability observation followed failure.",
        )
    elif name in {"controller-restart", "worker-restart"}:
        restart(history, operation_id, worker_restart=name == "worker-restart")
    elif name == "independent-work-during-human-review":
        need(
            bool(records) and records[0].get("claim_released") is True,
            "A has no verified released claim.",
        )
        first = records[0]
        merge = first.get("human_merge", {})
        need(
            merge.get("status") == "human_merged",
            "A still awaits its external human merge.",
        )
        require(
            history.run_id != first["run_id"]
            and history.item["issue_number"]
            not in {task["issue_number"] for task in history.scenario["tasks"]},
            "Independent work reused A or dependent B.",
        )
        start, end = timestamp(history.run["created_at"]), timestamp(
            history.run["finished_at"]
        )
        require(
            timestamp(first["finished_at"])
            < start
            < end
            < timestamp(merge["merged_at"]),
            "Independent work did not complete strictly within A's human-review interval.",
        )
        body = history.context["document"]["source"].get("body", "")
        import re

        require(
            not re.search(
                r"Depends on:\s*#"
                + str(history.scenario["tasks"][0]["issue_number"])
                + r"\b",
                body,
            ),
            "Independent work depends on unmerged A.",
        )
        history.ready(queue, github)
    if name in {"repair-exhaustion", "cancellation", "missing-capability"}:
        from .self_acceptance import pulls_for_branch

        branch = history.item["branch_name"]
        pulls = pulls_for_branch(github, history.scenario["repository"], branch)
        require(
            isinstance(pulls, list) and not pulls,
            "Failed scenario has a provider pull request.",
        )


def verify_failures(
    failures,
    worker=None,
    *,
    queue,
    github,
    scenario,
    records,
    operation_id,
    checkpoint_digests=(),
):
    identifier(operation_id)
    if not failures:
        return [
            "Missing actual failure-matrix evidence: " + name
            for name in sorted(FAILURE_CASES)
        ]
    require(
        isinstance(failures, dict)
        and set(failures) == {"schema_version", "operation_id", "cases"}
        and type(failures["schema_version"]) is int
        and failures["schema_version"] == 1
        and failures["operation_id"] == operation_id
        and isinstance(failures["cases"], dict)
        and set(failures["cases"]) <= FAILURE_CASES,
        "Unsupported or foreign failure-matrix observation.",
    )
    missing = []
    for name in sorted(FAILURE_CASES):
        pin = failures["cases"].get(name)
        if not pin or worker is None:
            missing.append("Missing actual failure-matrix evidence: " + name)
            continue
        require(
            isinstance(pin, dict)
            and not ({"passed", "fixtures", "receipts"} & set(pin)),
            "Scenario labels and termination-only evidence cannot prove factory behavior.",
        )
        try:
            need(
                all(
                    digest(point) in checkpoint_digests
                    for point in pin.get("checkpoints", [])
                ),
                "Checkpoint has not been authenticated and pinned in the acceptance state.",
            )
            history = History(pin, queue=queue, worker=worker, scenario=scenario)
            verify_case(
                name,
                history,
                queue=queue,
                github=github,
                records=records,
                operation_id=operation_id,
            )
        except IncompleteEvidence as error:
            missing.append(name + ": " + str(error))
    return missing
