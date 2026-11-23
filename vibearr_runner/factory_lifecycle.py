"""Factory role sessions reuse the native claim, workspace and operation journal."""

from copy import deepcopy
import hashlib
import json
from uuid import uuid4

from .factory_contract import FactoryContractError, artifact_document, validate_payload
from .factory_roles import prompt, retain_criteria, retain_requested_criteria
from .native_contract import agent_intent, agent_result, digest, plan_document
from .native_sessions import NativeSession, NativeSessionError
from .native_validation import CandidateSource
from .native_workspace import artifact_file, save_artifact
from .recovery import ReconciliationRequired
from .workspaces import git


class FactoryLifecycle:
    def __init__(self, host):
        self.host = host
        self.state = host.native.setdefault(
            "factory",
            {
                "schema_version": 1,
                "stage": "intake",
                "round": 1,
                "roles": {},
                "artifacts": {},
            },
        )

    @property
    def configuration(self):
        return self.host.snapshot["effective"]["factory"]

    def source(self):
        checkout = self.host.workspace().checkout
        return CandidateSource.from_git(checkout, git(["rev-parse", "HEAD"], checkout))

    def specification_payload(self):
        return self.state["artifacts"]["specification"]["document"]["payload"]

    def recovery(self):
        from .factory_recovery import FactoryRecovery

        return FactoryRecovery(self.host, self.state)

    def step(self):
        if self.state.get("schema_version") != 1:
            raise ReconciliationRequired(
                "Unsupported factory journal; retain the original run."
            )
        stage = self.state["stage"]
        if stage == "intake":
            previous = self.state.get("previous_specification")
            payload = {"previous_specification": previous} if previous else None
            artifact = self.run_role("intake", key="intake", payload=payload)
            if artifact is None:
                return "running"
            result = artifact["document"]["payload"]
            retain_requested_criteria(self.host.native["context"]["document"], result)
            if previous and result["verdict"] == "accept":
                retain_criteria(previous["document"]["payload"], result)
            if result["verdict"] != "accept":
                outcome = (
                    "needs_info" if result["verdict"] == "needs_info" else "rejected"
                )
                self.host.native.update(
                    outcome=outcome, failure=f"Factory {outcome}: {result['rationale']}"
                )
                self.state["stage"] = "finished"
                self.host.save()
                return self.host.finish_failure()
            self.state["stage"] = "specification"
            self.host.save()
            return "continue"
        if stage == "specification":
            intake = self.state["artifacts"]["intake"]
            artifact = self.run_role(
                "specification", key="specification", payload={"intake": intake}
            )
            if artifact is None:
                return "running"
            result = artifact["document"]["payload"]
            retain_criteria(intake["document"]["payload"], result)
            self.save_plan(result)
            self.state["specification_payload"] = result
            self.state["stage"] = "build"
            self.host.native["phase"] = "factory"
            self.host.save()
            return "continue"
        from .factory_reviews import FactoryReviewLoop

        return FactoryReviewLoop(self).step()

    def save_plan(self, payload):
        context = self.host.native["context"]
        criteria = [
            (
                entry
                if isinstance(entry, str)
                else entry.get("description", entry.get("text"))
            )
            for entry in payload["criteria"]
        ]
        document = plan_document(
            context_id=context["id"],
            context_digest=context["digest"],
            problem=context["document"]["source"]["title"],
            approach=payload["scope"] + "\n\n" + "\n".join(payload["verification"]),
            acceptance_criteria=criteria,
            factory_specification=self.state["artifacts"]["specification"],
        )
        request = self.state.setdefault(
            "plan_request", {"operation_id": str(uuid4()), "document": document}
        )
        if request["document"] != document:
            raise ReconciliationRequired(
                "Frozen factory plan changed before acknowledgement."
            )
        self.host.save()
        saved = self.host.metadata_request(
            f"/api/v1/runner/runs/{self.host.run_id}/plans/",
            {
                "claim_token": self.host.token,
                **request,
                "reason": "Actual factory specification and acceptance criteria.",
            },
        )["plan"]
        if (
            saved.get("document") != request["document"]
            or saved.get("presentation_only")
            or saved.get("unavailable")
        ):
            raise ReconciliationRequired(
                "Saved factory specification plan differs from the original request."
            )
        self.host.native["plan"] = saved
        self.state["plan"] = saved
        self.host.save()
        return saved

    def save_artifact(self, role, payload, *, key, execution=None, source=None):
        source = source or self.source()
        context = self.host.native["context"]
        plan = self.host.native.get("plan")
        base_sha = context["document"]["repository"]["base_revision"]
        if role not in {"intake", "specification"}:
            base_sha = (
                self.host.native.get("candidate", {})
                .get("workspace", {})
                .get("base_revision", base_sha)
            )
        operation = self.state.setdefault("artifact_requests", {}).get(key)
        if operation is None:
            document = artifact_document(
                run_id=self.host.run_id,
                operation_id=str(uuid4()),
                role=role,
                round=self.state["round"],
                context_id=context["id"],
                context_digest=context["digest"],
                specification_id=plan["id"] if plan else None,
                specification_digest=plan["digest"] if plan else "",
                source_digest=source.digest,
                tree_sha=source.tree_sha,
                base_sha=base_sha,
                head_sha=(
                    "" if role in {"intake", "specification"} else source.commit_sha
                ),
                configuration_digest=digest(self.host.snapshot),
                payload=deepcopy(payload),
                **({"execution": execution} if execution is not None else {}),
            )
            operation = {
                "operation_id": document["operation_id"],
                "document": document,
                "reason": "Recorded factory " + role + " evidence.",
            }
            self.state["artifact_requests"][key] = operation
            self.host.save()
        elif (
            operation["document"]["payload"] != payload
            or operation["document"]["source_digest"] != source.digest
            or operation["document"]["context_id"] != context["id"]
        ):
            raise ReconciliationRequired(
                "Factory artifact changed before acknowledgement."
            )
        saved = self.host.metadata_request(
            f"/api/v1/runner/runs/{self.host.run_id}/factory/artifacts/",
            {"claim_token": self.host.token, **operation},
        )["artifact"]
        if saved.get("document") != operation["document"] or saved.get(
            "digest"
        ) != digest(operation["document"]):
            raise ReconciliationRequired(
                "Factory artifact acknowledgement differs from the frozen evidence."
            )
        self.state["artifacts"][key] = saved
        self.host.save()
        return saved

    def run_role(self, role, *, key, payload=None, source=None, next_stage=None):
        from .native_conformance import (
            FactoryNativeAdapter,
            require_factory_capabilities,
        )

        source = source or self.source()
        self.recovery()
        previous = self.state["artifacts"].get(key)
        if previous is not None:
            context = self.host.native["context"]
            plan = (
                self.host.native.get("plan")
                if role not in {"intake", "specification"}
                else None
            )
            expected = {
                "role": role,
                "round": self.state["round"],
                "context_id": context["id"],
                "context_digest": context["digest"],
                "source_digest": source.digest,
                "specification_id": plan["id"] if plan else None,
                "specification_digest": plan["digest"] if plan else "",
            }
            if any(
                previous["document"].get(field) != value
                for field, value in expected.items()
            ):
                raise ReconciliationRequired(
                    "Saved factory role belongs to a different current binding."
                )
            return previous
        data = self.state["roles"].setdefault(key, {})
        capabilities = self.host.executor.capabilities()
        selected = require_factory_capabilities(capabilities, [role])[role]
        frozen = self.host.native["context"]["document"]["factory"]["profiles"].get(
            role
        )
        if (
            frozen is None
            or {k: v for k, v in frozen.items() if k != "registration"} != selected
        ):
            raise ReconciliationRequired(
                "Factory role registration changed; reconcile the original worker boot."
            )
        account = self.host.native["context"]["document"]["account"]
        underlying = self.host.registry.create(account["backend"], self.host.executor)
        adapter = FactoryNativeAdapter(underlying, role, capabilities)
        if not data.get("request"):
            text, prompt_digest, input_digest = prompt(
                role,
                self.host.native["context"]["document"],
                self.configuration,
                payload,
            )
            deadline = self.state.setdefault(
                "agent_deadline",
                self.host.stage_deadline("implementation_timeout_seconds"),
            )
            data["request"] = {
                "operation_id": str(uuid4()),
                "deadline": deadline,
                "source_commit": source.commit_sha,
                "prompt": text,
                "prompt_digest": prompt_digest,
                "input_digest": input_digest,
                "source_digest": source.digest,
                "round": self.state["round"],
                "browser_capture": deepcopy((payload or {}).get("browser_capture")),
                "specification_id": self.host.native.get("plan", {}).get("id"),
                "specification_digest": self.host.native.get("plan", {}).get(
                    "digest", ""
                ),
            }
            save_artifact(
                self.host.workspace(),
                data["request"]["operation_id"],
                "input.tar",
                source.archive,
            )
            self.host.save()
        request = data["request"]
        if request["source_digest"] != source.digest:
            raise ReconciliationRequired(
                "Factory role source changed while its original operation was pending."
            )
        original = artifact_file(
            self.host.workspace(), request["operation_id"], "input.tar"
        ).read_bytes()
        if hashlib.sha256(original).hexdigest() != request["source_digest"]:
            raise ReconciliationRequired(
                "Original factory role input is missing or changed."
            )
        request["deadline"] = self.recovery().begin_role(
            role,
            request["operation_id"],
            request["deadline"],
            binding={
                key: request[key]
                for key in ("source_digest", "specification_id", "specification_digest")
            },
        )
        self.host.save()
        self.host.resources.reserve(self.host.run_id, request["operation_id"])
        if not data.get("session"):
            self.host.require_human_dispatch()
            grant = self.host.secret("implementation")
            if (
                grant["credential"]["id"] != account["credential_id"]
                or grant["credential"]["version"] != account["credential_version"]
            ):
                raise NativeSessionError(
                    "Factory backend credential changed; retry with a new context."
                )
            options = (
                {"browser_capture": request["browser_capture"]}
                if request["browser_capture"] is not None
                else {}
            )
            session = adapter.prepare(
                run_id=self.host.run_id,
                operation_id=request["operation_id"],
                source=original,
                prompt=request["prompt"],
                model=self.configuration["roles"][role]["model"],
                policy=self.host.policy.to_data(),
                deadline=request["deadline"],
                credential=grant,
                **options,
            )
            self.host.resources.operation(
                self.host.run_id, session.manifest, kind="agent"
            )
            data["session"] = session.to_data()
            request["native_input_digest"] = session.manifest["context_digest"]
            self.host.save()
        session = NativeSession.from_data(data["session"])
        if request.get("native_input_digest") != session.manifest["context_digest"]:
            raise ReconciliationRequired("Frozen factory native input binding changed.")
        descriptor = {
            "role": role,
            "round": request["round"],
            "prompt_digest": request["prompt_digest"],
            "input_digest": request["native_input_digest"],
            "specification_id": request["specification_id"],
            "specification_digest": request["specification_digest"],
        }
        intent = agent_intent(
            **self.host.context_binding(),
            session=session,
            workspace_id=self.host.native.get("workspace_id"),
            factory=descriptor,
        )
        data["intent_id"] = self.host.intent(
            intent, "factory-intent:" + request["operation_id"]
        )
        self.host.save()
        observed = adapter.reconcile(session)
        self.recovery().observe_role(request["operation_id"], observed)
        if observed["state"] == "not-started":
            self.host.require_human_dispatch()
            if not self.recovery().before_dispatch(request["operation_id"]):
                return None
            data["start_intent"] = True
            self.host.save()
            self.host.resources.dispatch(self.host.run_id, request["operation_id"])
            adapter.start(session)
            return None
        if observed["state"] == "running":
            events = adapter.events(session)
            self.host.status(
                "implementation",
                detail=str(events[-1])[:2000] if events else "Factory role is running.",
            )
            return None
        if observed["state"] != "exited":
            raise ReconciliationRequired(
                "Factory role outcome is unknown; retain its operation fence."
            )
        outcome = adapter.outcome(session, original)
        if outcome.get("successful") is not True:
            raise NativeSessionError("Factory role did not finish successfully.")
        result = agent_result(
            intent_id=data["intent_id"],
            session=session,
            outcome=outcome,
            observed=observed,
        )
        data["result_id"] = self.host.artifact(
            "native_agent", result, "factory-result:" + request["operation_id"]
        )
        payload = outcome["role_result"]
        try:
            validate_payload(role, payload)
        except FactoryContractError as exc:
            raise NativeSessionError(str(exc)) from None
        if role != "build" and outcome["source"] != original:
            raise NativeSessionError("Read-only factory role changed candidate source.")
        save_artifact(
            self.host.workspace(),
            request["operation_id"],
            "output.tar",
            outcome["source"],
        )
        save_artifact(
            self.host.workspace(),
            request["operation_id"],
            "result.json",
            json.dumps(outcome["artifact"], sort_keys=True).encode(),
        )
        receipt = result["receipt"]
        data.update(
            successful=True,
            session_id=outcome["session_id"],
            agent_version=outcome["agent_version"],
            log_digest=outcome["log_digest"],
            export_digest=outcome["artifact"]["result_digest"],
            role_result=payload,
        )
        data["execution"] = {
            "intent_id": data["intent_id"],
            "result_id": data["result_id"],
            "adapter": session.adapter,
            "model": session.account["model"],
            "agent_version": session.agent_version,
            "session_id": outcome["session_id"],
            "operation_id": request["operation_id"],
            "execution_identity": session.execution_identity,
            "input_source_digest": session.manifest["source_digest"],
            "input_digest": request["native_input_digest"],
            "prompt_digest": request["prompt_digest"],
            "started_at": receipt["started_at"],
            "ended_at": receipt["ended_at"],
        }
        self.host.save()
        if role == "build":
            return {"build_complete": True, "operation_id": request["operation_id"]}
        saved = self.save_artifact(
            role, payload, key=key, execution=data["execution"], source=source
        )
        if next_stage is not None:
            self.state["stage"] = next_stage
            self.host.save()
        return saved

    def reviews(self):
        self.state["stage"] = "review"
        self.host.native["phase"] = "factory"
        self.host.save()
        return "continue"
