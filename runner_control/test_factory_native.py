"""Owned API checks for pre-plan factory sessions and immutable specifications."""

import copy
from unittest.mock import patch
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings

from vibearr_runner.test_factory_spec_sessions import BASE_ROLES, RoleExecutor
from credentials.test_models import TEST_KEY
from vibearr_executor.artifacts import digest, pack
from vibearr_runner import native_contract as contract
from vibearr_runner.factory_roles import freeze_context
from vibearr_runner.factory_routing import route
from vibearr_runner.native_codex import CODEX_VERSION
from vibearr_runner.native_sessions import NativeSession
from work_queue.models import QueueItem
from work_queue.services import refresh_issue_for_launch

from . import test_native_protocol as native_fixture
from . import test_factory_artifacts as artifact_fixture
from .factory_native import candidate_binding, require_artifact_binding
from .factory_services import save_artifact
from .native_services import selected_account, update_status
from .plan_services import save_context, save_plan
from .services import RunConflict, finish_run


class FactoryNativeFixture(native_fixture.NativeProtocolMixin):
    def setUp(self):
        super().setUp()
        self.executor = RoleExecutor(self.profile)
        self.worker_identity = self.executor.identity()
        self.configuration = {
            "schema_version": 1,
            "enabled": True,
            "roles": {
                role: {"model": "role-model", "instructions": ""} for role in BASE_ROLES
            },
        }
        self.run.configuration_snapshot["effective"]["factory"] = self.configuration
        self.run.save(update_fields=["configuration_snapshot"])
        self.criterion = "Pass the independent required validation checks."
        self.intake = {
            "verdict": "accept",
            "scope": "Implement the requested behavior",
            "criteria": [{"id": "requested", "description": self.criterion}],
            "rationale": "The request is clear",
            "risk": "low",
            "surfaces": [],
            "questions": [],
        }
        self.specification = {
            "scope": self.intake["scope"],
            "criteria": copy.deepcopy(self.intake["criteria"]),
            "exclusions": [],
            "verification": ["Run the independent checks"],
        }
        observation = {
            "repository_id": 123,
            "owner_id": 456,
            "repository_url": self.project.repository_url,
            "default_branch": "main",
        }
        with patch("work_queue.github.repository_metadata", return_value=observation):
            refresh_issue_for_launch(
                item_id=self.item.pk,
                claim_token=self.claim,
                token="selected",
                runner=self.runner,
                run_id=self.run.pk,
                mode="initial",
            )
        self.run.refresh_from_db()
        self.item.refresh_from_db()

    def context_document(self):
        return contract.context_document(
            run_id=str(self.run.pk),
            source=self.run.source_snapshot,
            repository={
                "repository_id": 123,
                "repository": "example/configured",
                "repository_url": self.project.repository_url,
                "base_branch": "main",
                "base_revision": "c" * 40,
                "base_tree_sha": "d" * 40,
                "branch": self.item.branch_name,
            },
            source_digest=digest(self.original),
            policy_digest=self.run.configuration_snapshot["policy_digest"],
            account=selected_account(self.run),
            validation_profile=self.profile,
            runtime_binding_digest="e" * 64,
            acceptance_criteria=[self.criterion],
            factory=freeze_context(self.configuration, self.executor.capabilities()),
        )

    def save_context(self, document=None):
        document = document or self.context_document()
        self.context = save_context(
            runner=self.runner,
            run_id=self.run.pk,
            claim_token=self.claim,
            operation_id=uuid4(),
            document=document,
            base_commit=document["repository"]["base_revision"],
            resolved_base_branch=document["repository"]["base_branch"],
            reason="Refresh the remote base" if self.run.contexts.exists() else "",
        )
        return self.context

    def common(self):
        return {
            "context_id": str(self.context.pk),
            "context_digest": self.context.digest,
            "policy_digest": self.run.configuration_snapshot["policy_digest"],
            "attempt": self.context.document.get("factory_generation", {}).get(
                "attempt", 0
            ),
        }

    def complete_agent(self):
        if not hasattr(self, "workspace_entry"):
            super().complete_agent()

    def role_intent(self, role="intake", *, specification=None, workspace=None):
        profile = self.executor.role_profiles[role]
        manifest = {
            "schema_version": 1,
            "run_id": str(self.run.pk),
            "operation_id": str(uuid4()),
            "profile_id": profile.profile_id,
            "profile_digest": profile.digest,
            "source_digest": (
                workspace.payload["source_digest"]
                if workspace
                else self.context.document["source_digest"]
            ),
            "context_digest": "1" * 64,
            "phase": "agent",
            "limits": profile.limits.__dict__,
            "deadline": self.deadline,
            "network_hosts": list(profile.network_hosts),
        }
        account = {**selected_account(self.run), "model": "role-model"}
        session = NativeSession(
            "codex",
            CODEX_VERSION,
            account,
            "login",
            profile.image,
            "e" * 64,
            manifest,
            self.worker_identity,
        )
        descriptor = {
            "role": role,
            "round": self.common()["attempt"] + 1,
            "prompt_digest": "f" * 64,
            "input_digest": manifest["context_digest"],
            "specification_id": str(specification.pk) if specification else None,
            "specification_digest": specification.digest if specification else "",
        }
        return contract.agent_intent(
            **self.common(),
            session=session,
            factory=descriptor,
            workspace_id=str(workspace.operation_id) if workspace else None,
        )

    def complete_candidate(self, plan):
        intent = self.write(
            "native_intent", self.role_intent("build", specification=plan)
        )
        self.intent_ids.append(str(intent.operation_id))
        session = NativeSession.from_data(intent.payload["data"]["session"])
        self.agent_intent = intent
        self.session = session
        observed = self.observed(
            session.manifest["operation_id"], self.original, self.changed
        )
        self.operations.append(observed)
        self.write(
            "native_agent",
            contract.agent_result(
                intent_id=str(intent.operation_id),
                session=session,
                observed=observed,
                outcome={
                    "result": observed["result"],
                    "artifact": observed["result"]["artifact"],
                    "source": self.changed,
                    "successful": True,
                    "session_id": "ses_builder",
                    "agent_version": CODEX_VERSION,
                },
            ),
        )
        self.workspace = {
            "repository_id": 123,
            "repository": "example/configured",
            "branch": self.item.branch_name,
            "base_branch": "main",
            "base_revision": self.context.document["repository"]["base_revision"],
            "tree_sha": ("8" if self.common()["attempt"] else "4") * 40,
            "commit_sha": ("9" if self.common()["attempt"] else "5") * 40,
        }
        self.workspace_entry = self.write(
            "native_workspace",
            contract.workspace_record(
                context_id=str(self.context.pk),
                context_digest=self.context.digest,
                attempt=self.common()["attempt"],
                agent_intent_id=str(intent.operation_id),
                workspace=self.workspace,
                source_digest=digest(self.changed),
                export_digest=digest(self.changed),
                requirements_digest=self.profile.dependency_digest,
                factory_paths=["file.py"],
            ),
        )
        return self.workspace_entry

    def complete_role(self, role, payload, *, specification=None, workspace=None):
        intent = self.write(
            "native_intent",
            self.role_intent(role, specification=specification, workspace=workspace),
        )
        self.intent_ids.append(str(intent.operation_id))
        session = NativeSession.from_data(intent.payload["data"]["session"])
        source = self.changed if workspace else self.original
        observed = self.observed(session.manifest["operation_id"], source, source)
        self.operations.append(observed)
        result = self.write(
            "native_agent",
            contract.agent_result(
                intent_id=str(intent.operation_id),
                session=session,
                observed=observed,
                outcome={
                    "result": observed["result"],
                    "artifact": observed["result"]["artifact"],
                    "source": source,
                    "successful": True,
                    "session_id": "ses_" + role,
                    "agent_version": CODEX_VERSION,
                },
            ),
        )
        if workspace:
            return self.save_candidate_artifact(
                role, payload, intent=intent, result=result
            )
        receipt = result.payload["receipt"]
        document = {
            "schema_version": 1,
            "run_id": str(self.run.pk),
            "operation_id": str(uuid4()),
            "role": role,
            "round": self.common()["attempt"] + 1,
            "context_id": str(self.context.pk),
            "context_digest": self.context.digest,
            "specification_id": None,
            "specification_digest": "",
            "source_digest": self.context.document["source_digest"],
            "tree_sha": self.context.document["repository"]["base_tree_sha"],
            "base_sha": self.context.document["repository"]["base_revision"],
            "head_sha": "",
            "configuration_digest": contract.digest(self.run.configuration_snapshot),
            "payload": payload,
            "execution": {
                "intent_id": str(intent.operation_id),
                "result_id": str(result.operation_id),
                "adapter": session.adapter,
                "model": session.account["model"],
                "agent_version": CODEX_VERSION,
                "session_id": result.payload["session_id"],
                "operation_id": session.manifest["operation_id"],
                "execution_identity": receipt["execution_identity"],
                "input_source_digest": digest(self.original),
                "input_digest": session.manifest["context_digest"],
                "prompt_digest": "f" * 64,
                "started_at": receipt["started_at"],
                "ended_at": receipt["ended_at"],
            },
        }
        return save_artifact(
            runner=self.runner,
            run_id=self.run.pk,
            claim_token=self.claim,
            operation_id=document["operation_id"],
            document=document,
        )

    def save_candidate_artifact(self, role, payload, *, intent=None, result=None):
        plan = self.run.plans.filter(context=self.context).last()
        document = {
            "schema_version": 1,
            "run_id": str(self.run.pk),
            "operation_id": str(uuid4()),
            "role": role,
            "round": self.common()["attempt"] + 1,
            "context_id": str(self.context.pk),
            "context_digest": self.context.digest,
            "specification_id": str(plan.pk),
            "specification_digest": plan.digest,
            "source_digest": self.workspace_entry.payload["source_digest"],
            "tree_sha": self.workspace["tree_sha"],
            "base_sha": self.workspace["base_revision"],
            "head_sha": self.workspace["commit_sha"],
            "configuration_digest": contract.digest(self.run.configuration_snapshot),
            "payload": payload,
        }
        if intent:
            session = NativeSession.from_data(intent.payload["data"]["session"])
            receipt = result.payload["receipt"]
            document["execution"] = {
                "intent_id": str(intent.operation_id),
                "result_id": str(result.operation_id),
                "adapter": session.adapter,
                "model": session.account["model"],
                "agent_version": result.payload["agent_version"],
                "session_id": result.payload["session_id"],
                "operation_id": session.manifest["operation_id"],
                "execution_identity": receipt["execution_identity"],
                "input_source_digest": session.manifest["source_digest"],
                "input_digest": session.manifest["context_digest"],
                "prompt_digest": intent.payload["data"]["factory"]["prompt_digest"],
                "started_at": receipt["started_at"],
                "ended_at": receipt["ended_at"],
            }
        return save_artifact(
            runner=self.runner,
            run_id=self.run.pk,
            claim_token=self.claim,
            operation_id=document["operation_id"],
            document=document,
        )

    def complete_specification(self):
        self.save_context()
        self.complete_role("intake", self.intake)
        return self.complete_role("specification", self.specification)

    def plan_document(self, specification):
        payload = specification.document["payload"]
        return contract.plan_document(
            context_id=str(self.context.pk),
            context_digest=self.context.digest,
            problem=self.context.document["source"]["title"],
            approach=payload["scope"] + "\n\n" + "\n".join(payload["verification"]),
            acceptance_criteria=[
                value if isinstance(value, str) else value["description"]
                for value in payload["criteria"]
            ],
            factory_specification={
                "id": str(specification.pk),
                "digest": specification.digest,
            },
        )

    def save_plan(self, document):
        return save_plan(
            runner=self.runner,
            run_id=self.run.pk,
            claim_token=self.claim,
            operation_id=uuid4(),
            document=document,
            reason="Actual specification",
        )


@override_settings(VIBEARR_CREDENTIAL_KEY=TEST_KEY)
class FactoryNativeTests(FactoryNativeFixture, TestCase):
    def test_intake_can_be_saved_before_any_plan(self):
        self.save_context()
        intent = self.write("native_intent", self.role_intent())
        self.assertEqual(intent.payload["data"]["factory"]["role"], "intake")
        self.assertFalse(self.run.plans.exists())

    def test_http_intake_dispatch_is_idempotent_before_any_plan(self):
        self.save_context()
        operation = str(uuid4())
        request = {
            "claim_token": str(self.claim),
            "operation_id": operation,
            "kind": "native_intent",
            "data": self.role_intent(),
        }
        first = self.post("runner_artifact", request)
        second = self.post("runner_artifact", request)
        self.assertEqual(first.status_code, 200, first.content)
        self.assertEqual(second.status_code, 200, second.content)
        self.assertEqual(
            first.json()["evidence"]["id"], second.json()["evidence"]["id"]
        )
        self.assertFalse(self.run.plans.exists())

    def test_enabled_factory_requires_frozen_qualified_context(self):
        document = self.context_document()
        document.pop("factory")
        with self.assertRaises(RunConflict):
            self.save_context(document)

    def test_factory_context_cannot_be_saved_when_disabled(self):
        document = self.context_document()
        self.run.configuration_snapshot["effective"]["factory"]["enabled"] = False
        self.run.save(update_fields=["configuration_snapshot"])
        with self.assertRaises(RunConflict):
            self.save_context(document)

    def test_preplan_builder_cannot_dispatch(self):
        self.save_context()
        with self.assertRaisesMessage(RunConflict, "before dispatch"):
            self.write("native_intent", self.role_intent("build"))

    def test_specification_requires_actual_accepted_intake(self):
        self.save_context()
        with self.assertRaisesMessage(RunConflict, "intake artifact"):
            self.write("native_intent", self.role_intent("specification"))

    def test_new_dispatch_rejects_historical_four_field_descriptor(self):
        self.save_context()
        document = self.role_intent()
        document["data"]["factory"].pop("specification_id")
        document["data"]["factory"].pop("specification_digest")
        with self.assertRaisesMessage(RunConflict, "frozen specification"):
            self.write("native_intent", document)

    def test_factory_role_model_must_match_frozen_selection(self):
        self.save_context()
        document = self.role_intent()
        document["data"]["session"]["account"]["model"] = "another-model"
        with self.assertRaisesMessage(RunConflict, "role/account/worker"):
            self.write("native_intent", document)

    def test_factory_role_input_digest_must_match_prepared_session(self):
        self.save_context()
        document = self.role_intent()
        document["data"]["factory"]["input_digest"] = "2" * 64
        with self.assertRaisesMessage(RunConflict, "role/account/worker"):
            self.write("native_intent", document)

    def test_factory_role_worker_boot_must_match_qualification(self):
        self.save_context()
        document = self.role_intent()
        document["data"]["session"]["execution_identity"]["worker_boot"] = str(uuid4())
        with self.assertRaises((ValidationError, RunConflict)):
            self.write("native_intent", document)

    def test_replaced_intake_operation_is_rejected_even_after_success(self):
        self.save_context()
        self.complete_role("intake", self.intake)
        with self.assertRaisesMessage(RunConflict, "original factory role"):
            self.write("native_intent", self.role_intent())

    def test_unconfirmed_result_cannot_clear_pending_intake(self):
        self.save_context()
        intent = self.write("native_intent", self.role_intent())
        session = NativeSession.from_data(intent.payload["data"]["session"])
        observed = self.observed(
            session.manifest["operation_id"], self.original, self.original
        )
        document = contract.agent_result(
            intent_id=str(intent.operation_id),
            session=session,
            observed=observed,
            outcome={
                "result": observed["result"],
                "artifact": observed["result"]["artifact"],
                "source": self.original,
                "successful": True,
                "session_id": "ses_role",
                "agent_version": CODEX_VERSION,
            },
        )
        document["receipt"]["descendants_terminated"] = False
        with self.assertRaises(ValidationError):
            self.write("native_agent", document)
        self.assertFalse(self.run.evidence.filter(kind="native_agent").exists())
        with self.assertRaisesMessage(RunConflict, "original factory role"):
            self.write("native_intent", self.role_intent())

    def test_plan_is_bound_to_actual_saved_specification(self):
        specification = self.complete_specification()
        plan = self.save_plan(self.plan_document(specification))
        self.assertEqual(
            plan.document["factory_specification_id"], str(specification.pk)
        )
        self.assertEqual(plan.document["acceptance_criteria"], [self.criterion])
        intent = self.write(
            "native_intent", self.role_intent("build", specification=plan)
        )
        self.assertEqual(
            intent.payload["data"]["factory"]["specification_id"], str(plan.pk)
        )

    def test_http_plan_binds_actual_saved_specification(self):
        specification = self.complete_specification()
        response = self.post(
            "runner_plans",
            {
                "claim_token": str(self.claim),
                "operation_id": str(uuid4()),
                "document": self.plan_document(specification),
                "reason": "Actual role output",
            },
        )
        self.assertEqual(response.status_code, 200, response.content)
        document = response.json()["plan"]["document"]
        self.assertEqual(document["factory_specification_id"], str(specification.pk))
        self.assertEqual(document["factory_specification_digest"], specification.digest)

    def test_plan_cannot_replace_actual_specification_criteria(self):
        specification = self.complete_specification()
        document = self.plan_document(specification)
        document["acceptance_criteria"] = ["A narrower unrelated task"]
        with self.assertRaisesMessage(RunConflict, "actual saved specification"):
            self.save_plan(document)

    def test_plan_cannot_claim_an_unsaved_specification(self):
        specification = self.complete_specification()
        document = self.plan_document(specification)
        document["factory_specification_id"] = str(uuid4())
        with self.assertRaisesMessage(RunConflict, "actual saved specification"):
            self.save_plan(document)

    def test_builder_cannot_dispatch_with_unbound_specification_pair(self):
        specification = self.complete_specification()
        self.save_plan(self.plan_document(specification))
        with self.assertRaisesMessage(RunConflict, "current specification"):
            self.write("native_intent", self.role_intent("build"))

    def test_builder_cannot_reuse_identical_superseded_plan_identity(self):
        specification = self.complete_specification()
        document = self.plan_document(specification)
        old = self.save_plan(document)
        current = self.save_plan(document)
        self.assertNotEqual(old.pk, current.pk)
        with self.assertRaisesMessage(RunConflict, "current specification"):
            self.write("native_intent", self.role_intent("build", specification=old))

    def test_reviewer_cannot_reuse_old_builder_after_equivalent_plan_revision(self):
        specification = self.complete_specification()
        document = self.plan_document(specification)
        original = self.save_plan(document)
        workspace = self.complete_candidate(original)
        current = self.save_plan(document)
        reviewer = self.role_intent(
            "reviewer-code", specification=current, workspace=workspace
        )
        with self.assertRaisesMessage(RunConflict, "original builder specification"):
            self.write("native_intent", reviewer)
        self.assertEqual(self.run.evidence.filter(kind="native_intent").count(), 3)

    def test_needs_info_finish_retry_survives_artifact_retention(self):
        self.save_context()
        self.intake.update(
            verdict="needs_info", questions=["What result should be returned?"]
        )
        self.complete_role("intake", self.intake)
        self.terminate()
        result = {
            "runner": self.runner,
            "run_id": self.run.pk,
            "claim_token": self.claim,
            "status": QueueItem.Status.FAILED,
            "outcome": "needs_info",
            "error_summary": "What result should be returned?",
        }
        completed = finish_run(**result)
        self.run.factory_artifacts.all()._raw_delete("default")
        retried = finish_run(**result)
        self.assertEqual(retried.pk, completed.pk)
        self.assertEqual(retried.native_outcome, "needs_info")

    def test_read_only_intake_cannot_import_a_candidate_workspace(self):
        self.save_context()
        self.complete_role("intake", self.intake)
        intent = self.run.evidence.get(kind="native_intent")
        document = contract.workspace_record(
            context_id=str(self.context.pk),
            context_digest=self.context.digest,
            attempt=0,
            agent_intent_id=str(intent.operation_id),
            workspace={
                "repository_id": 123,
                "repository": "example/configured",
                "branch": self.item.branch_name,
                "base_branch": "main",
                "base_revision": "c" * 40,
                "tree_sha": "4" * 40,
                "commit_sha": "5" * 40,
            },
            source_digest=digest(self.original),
            export_digest=digest(self.original),
            requirements_digest=self.profile.dependency_digest,
            factory_paths=["file.py"],
        )
        with self.assertRaisesMessage(RunConflict, "Only a factory builder"):
            self.write("native_workspace", document)


@override_settings(VIBEARR_CREDENTIAL_KEY=TEST_KEY)
class FactoryGenerationTests(FactoryNativeFixture, TestCase):
    def setUp(self):
        super().setUp()
        concrete = {
            "id": "concrete-task",
            "description": "Return the requested concrete result for file.py",
        }
        self.intake["criteria"].append(concrete)
        self.specification["criteria"].append(copy.deepcopy(concrete))
        specification = self.complete_specification()
        self.previous_plan = self.save_plan(self.plan_document(specification))
        self.previous_workspace = self.complete_candidate(self.previous_plan)
        self.previous_context = self.context
        self.new_source = pack(
            {
                "requirements.txt": (0o644, b"example==1.0\n"),
                "file.py": (0o644, b"new base\n"),
            }
        )

    def complete_validation(self):
        super().complete_validation()
        builder_result = self.run.evidence.get(
            kind="native_agent", payload__intent_id=str(self.agent_intent.operation_id)
        )
        criteria = [entry["id"] for entry in self.specification["criteria"]]
        self.save_candidate_artifact(
            "build",
            {
                "summary": "Implement every accepted criterion",
                "criteria": {identity: "Updated file.py" for identity in criteria},
            },
            intent=self.agent_intent,
            result=builder_result,
        )
        checks = self.save_candidate_artifact(
            "checks",
            {"state": "passed", "validation_id": str(self.validation.operation_id)},
        )
        routing_payload = route(
            self.configuration,
            self.intake,
            self.workspace_entry.payload["factory_paths"],
        )
        routing = self.save_candidate_artifact("routing", routing_payload)
        reviews = [
            self.complete_role(
                role,
                {"verdict": "pass", "findings": [], "coverage": criteria},
                specification=self.previous_plan,
                workspace=self.workspace_entry,
            )
            for role in routing_payload["required_reviewers"]
        ]
        self.complete_role(
            "decision",
            {
                "verdict": "ready",
                "rationale": "Every criterion is independently checked and reviewed",
                "artifact_ids": [
                    str(entry.pk) for entry in (checks, routing, *reviews)
                ],
            },
            specification=self.previous_plan,
            workspace=self.workspace_entry,
        )

    def generation_document(self):
        document = copy.deepcopy(self.previous_context.document)
        document["source_digest"] = digest(self.new_source)
        document["repository"].update(base_revision="6" * 40, base_tree_sha="7" * 40)
        document["factory_generation"] = {
            "previous_context_id": str(self.previous_context.pk),
            "previous_context_digest": self.previous_context.digest,
            "previous_workspace_id": str(self.previous_workspace.operation_id),
            "attempt": 1,
            "old_base": "c" * 40,
            "new_base": "6" * 40,
            "reason": "base_update",
        }
        return document

    def post_generation(self, document=None, *, remote="6" * 40, operation_id=None):
        document = document or self.generation_document()
        with patch("runner_control.publication_services.provider") as provider:
            provider.return_value.base_sha.return_value = remote
            response = self.post(
                "runner_contexts",
                {
                    "claim_token": str(self.claim),
                    "operation_id": str(operation_id or uuid4()),
                    "document": document,
                    "base_commit": document["repository"]["base_revision"],
                    "resolved_base_branch": "main",
                    "reason": "Observed changed remote base",
                },
            )
        if response.status_code == 200:
            self.context = self.run.contexts.last()
            self.original = self.new_source
        return response

    def test_new_base_runs_new_intake_specification_plan_and_builder(self):
        self.terminate()
        response = self.post_generation()
        self.assertEqual(response.status_code, 200, response.content)
        intake = self.complete_role("intake", self.intake)
        specification = self.complete_role("specification", self.specification)
        plan = self.save_plan(self.plan_document(specification))
        candidate = self.complete_candidate(plan)
        self.assertEqual(intake.round, 2)
        self.assertEqual(specification.round, 2)
        self.assertNotEqual(plan.pk, self.previous_plan.pk)
        self.assertEqual(candidate.payload["attempt"], 1)
        self.assertEqual(candidate.payload["workspace"]["base_revision"], "6" * 40)
        self.assertEqual(candidate.payload["context_id"], str(self.context.pk))

    def test_lost_generation_ack_replays_original_context_after_reference_moves(self):
        self.terminate()
        document = self.generation_document()
        operation_id = uuid4()
        first = self.post_generation(document, operation_id=operation_id)
        second = self.post_generation(document, operation_id=operation_id)
        self.assertEqual(first.status_code, 200, first.content)
        self.assertEqual(second.status_code, 200, second.content)
        self.assertEqual(first.json()["context"]["id"], second.json()["context"]["id"])
        self.assertEqual(self.run.contexts.count(), 2)

    def test_new_intake_cannot_drop_previous_concrete_criterion_before_specification(
        self,
    ):
        self.terminate()
        response = self.post_generation()
        self.assertEqual(response.status_code, 200, response.content)
        narrowed = {**self.intake, "criteria": self.intake["criteria"][:1]}
        self.complete_role("intake", narrowed)
        with self.assertRaisesMessage(ValidationError, "accepted criteria"):
            self.write("native_intent", self.role_intent("specification"))
        self.assertEqual(self.run.evidence.filter(kind="native_intent").count(), 4)

    def test_new_plan_cannot_drop_previous_concrete_criterion(self):
        self.terminate()
        response = self.post_generation()
        self.assertEqual(response.status_code, 200, response.content)
        self.complete_role("intake", self.intake)
        narrowed = {
            **self.specification,
            "criteria": self.specification["criteria"][:1],
        }
        specification = self.complete_role("specification", narrowed)
        with self.assertRaisesMessage(ValidationError, "accepted criteria"):
            self.save_plan(self.plan_document(specification))
        self.assertEqual(self.run.plans.count(), 1)

    def test_new_base_requires_explicit_generation(self):
        self.terminate()
        document = self.generation_document()
        document.pop("factory_generation")
        response = self.post_generation(document)
        self.assertEqual(response.status_code, 409, response.content)
        self.assertIn("explicit bounded base generation", response.json()["error"])

    def test_new_base_requires_current_previous_context(self):
        self.terminate()
        document = self.generation_document()
        document["factory_generation"]["previous_context_id"] = str(uuid4())
        response = self.post_generation(document)
        self.assertEqual(response.status_code, 409, response.content)
        self.assertIn("current context", response.json()["error"])

    def test_new_base_cannot_rewrite_frozen_task_source(self):
        self.terminate()
        document = self.generation_document()
        document["source"]["body"] = "Replace the requested task with another task"
        response = self.post_generation(document)
        self.assertEqual(response.status_code, 409, response.content)
        self.assertIn("immutable source", response.json()["error"])

    def test_new_base_must_match_independently_observed_remote(self):
        self.terminate()
        response = self.post_generation(remote="a" * 40)
        self.assertEqual(response.status_code, 409, response.content)
        self.assertIn("current remote base", response.json()["error"])

    def test_new_base_cannot_skip_shared_repair_attempt(self):
        self.terminate()
        document = self.generation_document()
        document["factory_generation"]["attempt"] = 2
        response = self.post_generation(document)
        self.assertEqual(response.status_code, 409, response.content)
        self.assertIn("consecutive shared repair allowance", response.json()["error"])

    def test_new_base_requires_confirmed_full_termination(self):
        response = self.post_generation()
        self.assertEqual(response.status_code, 409, response.content)
        self.assertFalse(self.run.contexts.filter(sequence=2).exists())

    def test_published_generation_requires_explicit_new_attempt(self):
        self.complete_delivery()
        self.assertTrue(
            self.run.evidence.filter(
                kind="native_delivery", payload__pull_request__number=9
            ).exists()
        )
        self.terminate()
        response = self.post_generation()
        self.assertEqual(response.status_code, 409, response.content)
        self.assertIn("Published factory work", response.json()["error"])


@override_settings(VIBEARR_CREDENTIAL_KEY=TEST_KEY)
class FactoryCandidateAuthorityTests(artifact_fixture.FactoryFixture, TestCase):
    factory_checks = True

    def saved_prerequisites(self):
        routing = self.save(self.document(role="routing"))
        checks_document = self.document(role="routing")
        checks_document["role"] = "checks"
        checks_document["payload"] = {
            "validation_id": str(self.validation.operation_id),
            "state": "passed",
        }
        checks = self.save(checks_document)
        workspace = {**self.workspace_entry.payload, "factory_paths": ["file.py"]}
        expected = candidate_binding(self.run, self.run.plans.last(), workspace)
        return [routing, checks], expected

    def test_saved_routing_and_checks_match_original_candidate(self):
        artifacts, expected = self.saved_prerequisites()
        require_artifact_binding(artifacts, expected)
        self.assertEqual({entry.role for entry in artifacts}, {"routing", "checks"})

    def test_saved_prerequisites_require_every_exact_candidate_binding(self):
        artifacts, expected = self.saved_prerequisites()
        different = {
            "specification_id": str(uuid4()),
            "specification_digest": "0" * 64,
            "source_digest": "0" * 64,
            "tree_sha": "0" * 40,
            "base_sha": "0" * 40,
            "head_sha": "0" * 40,
        }
        for key, value in different.items():
            with self.subTest(binding=key):
                changed = {**expected, key: value}
                with self.assertRaisesMessage(
                    RunConflict, "another specification or candidate"
                ):
                    require_artifact_binding(artifacts, changed)


@override_settings(VIBEARR_CREDENTIAL_KEY=TEST_KEY)
class FactoryRepairLimitTests(FactoryNativeFixture, TestCase):
    def configure_repairs(self, limit):
        self.configuration["max_repairs"] = limit
        self.run.configuration_snapshot["effective"]["factory"] = self.configuration
        self.run.save(update_fields=["configuration_snapshot"])

    def test_zero_limit_permits_original_intake_specification_and_build(self):
        self.configure_repairs(0)
        specification = self.complete_specification()
        plan = self.save_plan(self.plan_document(specification))
        candidate = self.complete_candidate(plan)
        self.assertEqual(candidate.payload["attempt"], 0)

    def test_zero_limit_rejects_repair_dispatch(self):
        self.configure_repairs(0)
        self.save_context()
        document = self.role_intent()
        document["attempt"] = 1
        document["data"]["factory"]["round"] = 2
        with self.assertRaisesMessage(RunConflict, "policy/repair limit"):
            self.write("native_intent", document)

    def test_one_limit_rejects_second_repair_dispatch(self):
        self.configure_repairs(1)
        self.save_context()
        document = self.role_intent()
        document["attempt"] = 2
        document["data"]["factory"]["round"] = 3
        with self.assertRaisesMessage(RunConflict, "policy/repair limit"):
            self.write("native_intent", document)

    def test_status_rejects_count_above_frozen_factory_limit(self):
        self.configure_repairs(0)
        with self.assertRaisesMessage(RunConflict, "accounting"):
            update_status(
                runner=self.runner,
                run_id=self.run.pk,
                claim_token=self.claim,
                data={"sequence": 1, "stage": "implementation", "repair_count": 1},
            )

    def test_status_accepts_count_at_frozen_factory_limit(self):
        self.configure_repairs(1)
        update_status(
            runner=self.runner,
            run_id=self.run.pk,
            claim_token=self.claim,
            data={"sequence": 1, "stage": "implementation", "repair_count": 1},
        )
        self.run.refresh_from_db()
        self.assertEqual(self.run.native_status["repair_count"], 1)

    def generation(self, limit):
        self.configure_repairs(limit)
        specification = self.complete_specification()
        self.previous_plan = self.save_plan(self.plan_document(specification))
        self.previous_workspace = self.complete_candidate(self.previous_plan)
        self.previous_context = self.context
        self.new_source = pack({"file.py": (0o644, b"new base\n")})
        self.terminate()
        return FactoryGenerationTests.generation_document(self)

    def test_zero_limit_rejects_base_generation(self):
        document = self.generation(0)
        response = FactoryGenerationTests.post_generation(self, document)
        self.assertEqual(response.status_code, 409, response.content)
        self.assertIn("shared repair allowance", response.json()["error"])

    def test_one_limit_permits_base_generation_and_new_intake(self):
        document = self.generation(1)
        response = FactoryGenerationTests.post_generation(self, document)
        self.assertEqual(response.status_code, 200, response.content)
        intake = self.complete_role("intake", self.intake)
        self.assertEqual(intake.round, 2)
