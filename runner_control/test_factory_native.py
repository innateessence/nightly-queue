"""Owned API checks for pre-plan factory sessions and immutable specifications."""

import copy
from unittest.mock import patch
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings

from coding_runner.test_factory_spec_sessions import BASE_ROLES, RoleExecutor
from credentials.test_models import TEST_KEY
from vibearr_executor.artifacts import digest
from vibearr_runner import native_contract as contract
from vibearr_runner.factory_roles import freeze_context
from vibearr_runner.native_codex import CODEX_VERSION
from vibearr_runner.native_sessions import NativeSession
from work_queue.models import QueueItem
from work_queue.services import refresh_issue_for_launch

from . import test_native_protocol as native_fixture
from . import test_factory_artifacts as artifact_fixture
from .factory_native import candidate_binding, require_artifact_binding
from .factory_services import save_artifact
from .native_services import selected_account
from .plan_services import save_context, save_plan
from .services import RunConflict, finish_run


@override_settings(NQ_CREDENTIAL_KEY=TEST_KEY)
class FactoryNativeTests(native_fixture.NativeProtocolMixin, TestCase):
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
        self.context = save_context(
            runner=self.runner,
            run_id=self.run.pk,
            claim_token=self.claim,
            operation_id=uuid4(),
            document=document or self.context_document(),
            base_commit="c" * 40,
            resolved_base_branch="main",
            reason="",
        )
        return self.context

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
                else digest(self.original)
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
            "token",
            profile.image,
            "e" * 64,
            manifest,
            self.worker_identity,
        )
        descriptor = {
            "role": role,
            "round": 1,
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
        return self.write(
            "native_workspace",
            contract.workspace_record(
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
                source_digest=digest(self.changed),
                export_digest=digest(self.changed),
                requirements_digest=self.profile.dependency_digest,
                factory_paths=["file.py"],
            ),
        )

    def complete_role(self, role, payload):
        intent = self.write("native_intent", self.role_intent(role))
        self.intent_ids.append(str(intent.operation_id))
        session = NativeSession.from_data(intent.payload["data"]["session"])
        observed = self.observed(
            session.manifest["operation_id"], self.original, self.original
        )
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
                    "source": self.original,
                    "successful": True,
                    "session_id": "ses_role",
                    "agent_version": CODEX_VERSION,
                },
            ),
        )
        receipt = result.payload["receipt"]
        document = {
            "schema_version": 1,
            "run_id": str(self.run.pk),
            "operation_id": str(uuid4()),
            "role": role,
            "round": 1,
            "context_id": str(self.context.pk),
            "context_digest": self.context.digest,
            "specification_id": None,
            "specification_digest": "",
            "source_digest": digest(self.original),
            "tree_sha": "d" * 40,
            "base_sha": "c" * 40,
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

    def complete_specification(self):
        self.save_context()
        self.complete_role("intake", self.intake)
        return self.complete_role("specification", self.specification)

    def plan_document(self, specification):
        return contract.plan_document(
            context_id=str(self.context.pk),
            context_digest=self.context.digest,
            problem=self.context.document["source"]["title"],
            approach=self.specification["scope"]
            + "\n\n"
            + "\n".join(self.specification["verification"]),
            acceptance_criteria=[self.criterion],
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


@override_settings(NQ_CREDENTIAL_KEY=TEST_KEY)
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
