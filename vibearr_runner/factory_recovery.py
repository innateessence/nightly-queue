"""Factory roles share native ownership, operation identity and execution limits."""

from copy import deepcopy
from datetime import datetime, timezone
import math
import time
from uuid import UUID

from .budgets import ActiveBudget, BudgetExceeded
from .native_contract import digest, receipt_summary
from .recovery import ReconciliationRequired


class FactoryRecovery:
    def __init__(self, native, factory_state, *, clock=time.time):
        self.native, self.factory, self.clock = native, factory_state, clock
        controls = native.snapshot["global"]
        stages = {
            stage: controls[f"{stage}_timeout_seconds"]
            for stage in ("research", "implementation", "validation")
        }
        self.budget = ActiveBudget(
            native.journal,
            native.state,
            native.run_id,
            controls["task_timeout_seconds"],
            stages,
        )
        self.limit = min(2, native.policy.repair_limit)
        frozen = {
            "repair_limit": self.limit,
            "deadline": native.native["deadline"],
            "account": deepcopy(native.native["context"]["document"]["account"]),
        }
        self.data = factory_state.setdefault(
            "recovery",
            {"schema_version": 1, "frozen": frozen, "operations": {}, "repairs": []},
        )
        if self.data.get("schema_version") != 1 or self.data.get("frozen") != frozen:
            raise ReconciliationRequired("Frozen factory execution controls changed.")
        if factory_state.get("round", 1) != native.native["attempt"] + 1:
            raise ReconciliationRequired(
                "Factory round differs from the shared repair allowance."
            )
        for operation_id, record in self.data["operations"].items():
            self.budget.charge(
                "implementation", operation_id, record["elapsed"], enforce=False
            )
        self.native.save()

    def _role_data(self, operation_id):
        matching = [
            value
            for value in self.factory.get("roles", {}).values()
            if value.get("request", {}).get("operation_id") == operation_id
        ]
        if len(matching) != 1:
            raise ReconciliationRequired(
                "Factory operation has no unique frozen role request."
            )
        return matching[0]

    def _operation(self, operation_id):
        record = self.data["operations"].get(operation_id)
        if record is None:
            raise ReconciliationRequired(
                "Register the role operation before dispatch or observation."
            )
        return record

    def _binding(self, role, operation_id, binding):
        request = self._role_data(operation_id)["request"]
        context = self.native.native["context"]
        return {
            "role": role,
            "round": self.factory.get("round", 1),
            "context_id": context["id"],
            "context_digest": context["digest"],
            "input": {
                key: request.get(key)
                for key in ("source_commit", "input_digest", "prompt_digest")
            },
            "binding": deepcopy(binding),
        }

    def begin_role(self, role, operation_id, deadline, *, binding=None):
        roles = {
            "intake",
            "specification",
            "build",
            "reviewer-code",
            "reviewer-security",
            "reviewer-ux",
            "reviewer-ui",
            "decision",
        }
        if role not in roles:
            raise ReconciliationRequired("Choose a registered factory agent role.")
        if role == "build" and self.factory.get("round", 1) > 1:
            repairs = self.data["repairs"]
            if not repairs or repairs[-1]["attempt"] != self.native.native["attempt"]:
                raise ReconciliationRequired(
                    "Repair build lacks its frozen failure evidence."
                )
        try:
            if str(UUID(operation_id)) != operation_id:
                raise ValueError
            requested = datetime.fromisoformat(deadline).timestamp()
            overall = datetime.fromisoformat(self.native.native["deadline"]).timestamp()
        except (TypeError, ValueError):
            raise ReconciliationRequired(
                "Invalid frozen role operation or deadline."
            ) from None
        frozen = self._binding(role, operation_id, binding)
        previous = self.data["operations"].get(operation_id)
        if previous:
            if (
                previous["binding"] != frozen
                or requested > previous["requested_deadline"]
            ):
                raise ReconciliationRequired("Frozen role binding or deadline changed.")
            return previous["deadline"]
        if any(
            record["state"] != "exited" for record in self.data["operations"].values()
        ):
            raise ReconciliationRequired(
                "Reconcile the original role before starting another."
            )
        now = self.clock()
        bounded = min(requested, overall, now + self.budget.remaining("implementation"))
        if bounded <= now:
            raise BudgetExceeded("Factory agent or overall deadline is exhausted.")
        value = datetime.fromtimestamp(bounded, timezone.utc).isoformat()
        self.data["operations"][operation_id] = {
            "binding": frozen,
            "requested_deadline": requested,
            "deadline": value,
            "state": "prepared",
            "dispatched_at": None,
            "elapsed": 0.0,
        }
        self.native.save()
        return value

    def before_dispatch(self, operation_id):
        record = self._operation(operation_id)
        if record["state"] == "exited":
            raise ReconciliationRequired(
                "A completed factory role cannot be dispatched again."
            )
        if record.get("dispatched_at") is not None and not record.get(
            "verified_not_started"
        ):
            raise ReconciliationRequired(
                "Reconcile the original dispatch before retrying it."
            )
        if not self.native.checkpoint(force_source=True):
            return False
        frozen = record["binding"]
        if self._binding(frozen["role"], operation_id, frozen["binding"]) != frozen:
            raise ReconciliationRequired("Frozen role input changed before dispatch.")
        if self.clock() >= datetime.fromisoformat(record["deadline"]).timestamp():
            raise BudgetExceeded("Factory role deadline is exhausted.")
        session = self._role_data(operation_id).get("session")
        if not isinstance(session, dict):
            raise ReconciliationRequired(
                "A prepared factory session is required before dispatch."
            )
        manifest = session.get("manifest", {})
        if (
            manifest.get("operation_id") != operation_id
            or manifest.get("run_id") != self.native.run_id
        ):
            raise ReconciliationRequired(
                "Prepared factory session belongs to another operation."
            )
        selected = {
            "manifest": deepcopy(manifest),
            "execution_identity": deepcopy(session.get("execution_identity")),
        }
        if record.get("session") not in (None, selected):
            raise ReconciliationRequired("Prepared factory session identity changed.")
        record["session"] = selected
        if record["dispatched_at"] is None:
            record["dispatched_at"] = self.clock()
        record.update(state="dispatching", verified_not_started=False)
        self.native.save()
        return True

    def _charge(self, operation_id, record, elapsed, *, enforce=True):
        if (
            type(elapsed) not in {int, float}
            or not math.isfinite(elapsed)
            or elapsed < 0
        ):
            raise ReconciliationRequired("Factory operation elapsed time is invalid.")
        record["elapsed"] = max(record["elapsed"], elapsed)
        self.native.save()
        self.budget.charge(
            "implementation", operation_id, record["elapsed"], enforce=enforce
        )

    def _verify_identity(self, record, observed):
        expected = record.get("session", {}).get("execution_identity")
        if expected is not None and observed.get("execution_identity") != expected:
            raise ReconciliationRequired(
                "Factory status belongs to another worker instance or boot."
            )

    def _verify_not_started(self, operation_id, record):
        try:
            receipt = self.native.executor.termination_receipt(operation_id)
        except Exception:
            raise ReconciliationRequired(
                "Original factory dispatch presence remains uncertain."
            ) from None
        expected = {
            "operation_id": operation_id,
            "run_id": self.native.run_id,
            "state": "not-started",
            "manifest_digest": digest(record["session"]["manifest"]),
        }
        if any(receipt.get(key) != value for key, value in expected.items()):
            raise ReconciliationRequired(
                "Original factory dispatch lacks matching not-started proof."
            )
        identity = record["session"].get("execution_identity")
        if identity and receipt.get("instance_id") != identity["instance_id"]:
            raise ReconciliationRequired(
                "Original factory dispatch state was replaced."
            )

    def observe_role(self, operation_id, observed, *, enforce_budget=True):
        record = self._operation(operation_id)
        if observed.get("operation_id") != operation_id:
            raise ReconciliationRequired("Factory status belongs to another operation.")
        self._verify_identity(record, observed)
        state = observed.get("state")
        if state == "not-started":
            if record["state"] == "exited":
                raise ReconciliationRequired("Completed factory operation disappeared.")
            if record["dispatched_at"] is not None:
                self._verify_not_started(operation_id, record)
            record.update(state="prepared", verified_not_started=True)
            self.native.save()
            return False
        if state == "unknown" or state not in {"running", "exited"}:
            record["state"] = "unknown"
            self.native.save()
            raise ReconciliationRequired(
                "Factory outcome is unknown; retain its original operation and fence."
            )
        if state == "running":
            if record["dispatched_at"] is None:
                raise ReconciliationRequired(
                    "An unregistered factory launch cannot be adopted."
                )
            if record["state"] == "exited":
                raise ReconciliationRequired(
                    "Completed factory operation resumed unexpectedly."
                )
            record["state"] = "running"
            self._charge(
                operation_id,
                record,
                self.clock() - record["dispatched_at"],
                enforce=enforce_budget,
            )
            return False
        receipt = receipt_summary(observed)
        if receipt["run_id"] != self.native.run_id:
            raise ReconciliationRequired("Factory receipt belongs to another run.")
        if record.get("receipt_digest") not in (None, receipt["receipt_digest"]):
            raise ReconciliationRequired("Completed factory receipt changed.")
        started, ended = receipt["started_at"], receipt["ended_at"]
        if record["dispatched_at"] is None and (
            started is not None or receipt["canceled"] is not True
        ):
            raise ReconciliationRequired(
                "An unregistered factory launch cannot be adopted."
            )
        elapsed = (
            0.0 if started is None else ended - started if ended is not None else -1
        )
        if elapsed < 0:
            raise ReconciliationRequired("Factory receipt has invalid elapsed time.")
        record.update(state="exited", receipt_digest=receipt["receipt_digest"])
        self._charge(operation_id, record, elapsed, enforce=enforce_budget)
        return True

    def stop(self):
        observed = self.native.resources.reconcile(
            self.native.run_id, self.native.executor, stop=True
        )
        for operation in observed.get("operations", []):
            if (
                operation.get("state") != "exited"
                or operation.get("descendants_terminated") is not True
            ):
                raise ReconciliationRequired(
                    "Factory descendant termination remains uncertain."
                )
        for build in observed.get("builds", []):
            if build.get("resources_terminated") is not True:
                raise ReconciliationRequired(
                    "Factory dependency cleanup remains uncertain."
                )
        load = getattr(self.native.resources, "load", None)
        resources = load(self.native.run_id) if callable(load) else None
        for operation_id, record in self.data["operations"].items():
            if record["state"] == "exited":
                continue
            unprepared = (
                resources is not None
                and not record.get("session")
                and record["dispatched_at"] is None
                and operation_id not in resources.get("operations", {})
                and operation_id not in resources.get("pending", {})
            )
            if unprepared:
                record.update(state="exited", canceled_before_prepare=True)
                self.native.save()
                continue
            status = self.native.executor.status(operation_id)
            self.observe_role(operation_id, status, enforce_budget=False)
        return observed

    def _repair_evidence(self, reason):
        trigger = self.factory.get("repair_trigger")
        if reason not in {"checks", "reviews"} or not isinstance(trigger, dict):
            raise ReconciliationRequired(
                "Factory repair requires saved current failure evidence."
            )
        native = self.native.native
        context, plan, candidate = (
            native["context"],
            native.get("plan", {}),
            native.get("candidate", {}),
        )
        workspace = candidate.get("workspace", {})
        expected = {
            "reason": reason,
            "round": self.factory["round"],
            "source_digest": candidate.get("source_digest"),
            "specification_id": plan.get("id"),
            "specification_digest": plan.get("digest"),
        }
        if any(
            trigger.get(key) != value or value is None
            for key, value in expected.items()
        ):
            raise ReconciliationRequired("Factory repair trigger is missing or stale.")
        expected_document = {
            "run_id": self.native.run_id,
            "round": self.factory["round"],
            "context_id": context["id"],
            "context_digest": context["digest"],
            "source_digest": candidate.get("source_digest"),
            "tree_sha": workspace.get("tree_sha"),
            "head_sha": workspace.get("commit_sha"),
            "base_sha": workspace.get("base_revision"),
            "specification_id": plan.get("id"),
            "specification_digest": plan.get("digest"),
            "configuration_digest": digest(self.native.snapshot),
        }
        records = []

        def evidence(role):
            saved = self.factory.get("artifacts", {}).get(
                f"{role}:{self.factory['round']}"
            )
            document = saved.get("document") if isinstance(saved, dict) else None
            if not isinstance(document, dict) or document.get("role") != role:
                raise ReconciliationRequired(
                    "Factory repair lacks a required failure artifact."
                )
            if any(
                document.get(key) != value or value is None
                for key, value in expected_document.items()
            ):
                raise ReconciliationRequired(
                    "Factory repair artifact belongs to another candidate or specification."
                )
            if not saved.get("id") or saved.get("digest") != digest(document):
                raise ReconciliationRequired(
                    "Factory repair artifact acknowledgement changed."
                )
            records.append({"id": saved["id"], "digest": saved["digest"]})
            return document["payload"]

        checks = evidence("checks")
        validation_id = native.get("validation", {}).get("result_id")
        if not validation_id or checks.get("validation_id") != validation_id:
            raise ReconciliationRequired(
                "Factory repair checks lack their protected validation result."
            )
        if reason == "checks" and checks.get("state") != "failed":
            raise ReconciliationRequired(
                "Factory repair requires actual failed checks."
            )
        if reason == "reviews":
            if checks.get("state") != "passed":
                raise ReconciliationRequired("Review repair requires passing checks.")
            required = evidence("routing").get("required_reviewers")
            reviewers = {
                "reviewer-code",
                "reviewer-security",
                "reviewer-ux",
                "reviewer-ui",
            }
            if (
                not isinstance(required, list)
                or not required
                or "reviewer-code" not in required
                or len(set(required)) != len(required)
                or not set(required) <= reviewers
            ):
                raise ReconciliationRequired(
                    "Factory repair lacks the required reviewer set."
                )
            verdicts = [evidence(role).get("verdict") for role in required]
            if "changes" not in verdicts or not set(verdicts) <= {"pass", "changes"}:
                raise ReconciliationRequired(
                    "Factory repair requires completed current reviews requesting changes."
                )
        if trigger.get("artifact_ids") != [record["id"] for record in records]:
            raise ReconciliationRequired(
                "Factory repair trigger differs from its actual failure artifacts."
            )
        return {**deepcopy(trigger), "artifacts": records}

    def repair(self, reason, diagnostics):
        if self.native.native.get("delivery_recovery_generation"):
            raise BudgetExceeded(
                "Delivery-only recovery cannot launch factory repairs."
            )
        trigger = self.factory.get("repair_trigger")
        previous = self.data["repairs"][-1] if self.data["repairs"] else None
        if (
            previous
            and previous["trigger"] == trigger
            and previous["attempt"] == self.native.native["attempt"]
        ):
            return "continue"
        evidence = self._repair_evidence(reason)
        self.stop()
        if self.budget.remaining("implementation") <= 0:
            raise BudgetExceeded("Factory aggregate agent budget is exhausted.")
        attempt = self.native.native["attempt"]
        if attempt >= self.limit:
            raise BudgetExceeded("Factory shared repair allowance is exhausted.")
        self.data["repairs"].append(
            {
                "attempt": attempt + 1,
                "reason": str(reason)[:200],
                "diagnostics": str(diagnostics)[-16000:],
                "trigger": deepcopy(trigger),
                "evidence": evidence,
            }
        )
        self.native.native["attempt"] = attempt + 1
        self.native.native["repair_diagnostic"] = str(diagnostics)[-16000:]
        for key in ("agent", "validation", "dependency", "commit"):
            self.native.native.pop(key, None)
        for key in ("decision", "current_decision", "ready"):
            self.factory.pop(key, None)
        self.factory.update(round=attempt + 2, stage="build")
        self.native.native["phase"] = "factory"
        self.native.save()
        return "continue"

    def base_changed(self, observation):
        from .factory_generation import FactoryBaseUpdate

        return FactoryBaseUpdate(self.native).prepare(observation)
