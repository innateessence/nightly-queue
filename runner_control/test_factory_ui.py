from unittest.mock import patch
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from credentials.models import Credential
from credentials.test_models import TEST_KEY
from projects.models import Project
from vibearr_runner.native_contract import digest
from vibearr_runner.factory_routing import default_configuration, route
from work_queue.models import QueueItem

from .factory_models import FactoryArtifact
from .factory_presentation import prepare_factory_runs
from .models import ContextSnapshot, PlanVersion, Run, RunEvidence, RunnerIdentity


class FactoryUITests(TestCase):
    def setUp(self):
        self.project = Project.objects.create(
            name="Factory UI", repository_url="https://github.com/example/factory"
        )
        self.item = QueueItem.objects.create(
            project=self.project, title="Preserve queue filters", status="running"
        )
        self.runner = RunnerIdentity.objects.create(
            name="factory-ui", token_digest="x" * 64
        )
        factory = {**default_configuration(), "enabled": True}
        self.run = Run.objects.create(
            runner=self.runner,
            item=self.item,
            state="active",
            active_slot=1,
            stage="review",
            configuration_snapshot={
                "execution_kind": "native",
                "effective": {"factory": factory},
            },
        )
        self.context = ContextSnapshot.objects.create(
            run=self.run,
            operation_id=uuid4(),
            sequence=1,
            digest="1" * 64,
            reason="UI fixture",
            document={"execution_kind": "native", "title": self.item.title},
            inspected_base_commit="a" * 40,
            resolved_base_branch="main",
            discussion_digest="2" * 64,
        )
        self.intake = self.artifact(
            "intake",
            {
                "verdict": "accept",
                "scope": "Keep filter selection",
                "criteria": ["Filter selection survives navigation"],
                "rationale": "Preserve queue state",
                "risk": "medium",
                "surfaces": ["security"],
                "questions": [],
            },
        )
        specification = self.artifact(
            "specification",
            {
                "scope": "Keep filter selection",
                "criteria": ["Filter selection survives navigation"],
                "exclusions": [],
                "verification": ["Run the queue tests"],
            },
        )
        plan_document = {
            "execution_kind": "native",
            "problem": "Keep filter selection",
            "acceptance_criteria": ["Filter selection survives navigation"],
            "approach": "Preserve query parameters",
            "factory_specification_id": str(specification.pk),
            "factory_specification_digest": specification.digest,
        }
        self.plan = PlanVersion.objects.create(
            run=self.run,
            context=self.context,
            operation_id=uuid4(),
            version=1,
            digest=digest(plan_document),
            reason="UI fixture",
            document=plan_document,
        )
        Run.objects.filter(pk=self.run.pk).update(
            context_reference=str(self.context.pk), plan_reference=str(self.plan.pk)
        )
        self.run.refresh_from_db()

    def artifact(
        self, role, payload, *, round=1, tree="b" * 40, run=None, schema_version=1
    ):
        run = run or self.run
        run.refresh_from_db()
        plan = getattr(self, "plan", None)
        operation_id = uuid4()
        document = {
            "schema_version": schema_version,
            "run_id": str(run.pk),
            "operation_id": str(operation_id),
            "role": role,
            "round": round,
            "context_id": str(self.context.pk),
            "context_digest": self.context.digest,
            "specification_id": str(plan.pk) if plan else None,
            "specification_digest": plan.digest if plan else "",
            "source_digest": "4" * 64,
            "tree_sha": tree,
            "base_sha": "a" * 40,
            "head_sha": "c" * 40,
            "configuration_digest": digest(run.configuration_snapshot),
            "payload": payload,
        }
        if role not in {"routing", "checks"}:
            document["execution"] = {
                "intent_id": str(uuid4()),
                "result_id": str(uuid4()),
                "adapter": "codex",
                "model": "fixture-model",
                "agent_version": "fixture-cli",
                "session_id": str(uuid4()),
                "operation_id": str(uuid4()),
                "execution_identity": {
                    "runtime": "compose-slot-v2",
                    "protocol_version": 2,
                    "instance_id": str(uuid4()),
                    "worker_boot": str(uuid4()),
                    "binding_digest": "6" * 64,
                },
                "input_source_digest": "7" * 64,
                "input_digest": "8" * 64,
                "prompt_digest": "9" * 64,
                "started_at": 10.0,
                "ended_at": 12.5,
            }
        sequence = FactoryArtifact.objects.filter(run=run).count() + 1
        return FactoryArtifact.objects.create(
            run=run,
            context=self.context,
            specification=plan,
            operation_id=operation_id,
            sequence=sequence,
            role=role,
            round=round,
            document=document,
            digest=digest(document),
            reason="Disposable UI fixture",
        )

    def build(self, **kwargs):
        entry = self.artifact(
            "build",
            {
                "summary": "Preserve filter selection",
                "criteria": {"criterion-1": "Query links preserve state"},
            },
            **kwargs,
        )
        RunEvidence.objects.create(
            run=self.run,
            operation_id=uuid4(),
            kind="native_workspace",
            payload={
                "source_digest": entry.document["source_digest"],
                "context_id": str(self.context.pk),
                "context_digest": self.context.digest,
                "factory_paths": ["work_queue/services.py"],
                "workspace": {
                    "tree_sha": entry.document["tree_sha"],
                    "base_revision": entry.document["base_sha"],
                    "commit_sha": entry.document["head_sha"],
                },
            },
        )
        return entry

    def routing(self, **kwargs):
        return self.artifact(
            "routing",
            route(
                self.run.configuration_snapshot["effective"]["factory"],
                self.intake.document["payload"],
                ["work_queue/services.py"],
            ),
            **kwargs,
        )

    def checks(self, **kwargs):
        validation_id = uuid4()
        entry = self.artifact(
            "checks", {"validation_id": str(validation_id), "state": "passed"}, **kwargs
        )
        RunEvidence.objects.create(
            run=self.run,
            operation_id=validation_id,
            kind="native_validation",
            payload={
                "run_id": str(self.run.pk),
                "state": "passed",
                "source_digest": entry.document["source_digest"],
                "tree_sha": entry.document["tree_sha"],
            },
        )
        return entry

    def review(
        self,
        role="reviewer-code",
        verdict="pass",
        findings=None,
        coverage=None,
        **kwargs,
    ):
        return self.artifact(
            role,
            {
                "verdict": verdict,
                "findings": findings or [],
                "coverage": ["criterion-1"] if coverage is None else coverage,
            },
            **kwargs,
        )

    def complete_round(self):
        self.build()
        entries = [
            self.checks(),
            self.routing(),
            self.review(),
            self.review("reviewer-security"),
        ]
        self.artifact(
            "decision",
            {
                "verdict": "ready",
                "rationale": "Checks and required reviews passed",
                "artifact_ids": [str(entry.pk) for entry in entries],
            },
        )

    def detail(self, **query):
        return self.client.get(reverse("runner_plan_detail", args=[self.run.pk]), query)

    def delivered_publication(self):
        url = "https://github.com/example/factory/pull/3"
        Run.objects.filter(pk=self.run.pk).update(
            state="completed",
            active_slot=None,
            result_status="succeeded",
            native_outcome="delivered",
            result_url=url,
        )
        RunEvidence.objects.create(
            run=self.run,
            operation_id=uuid4(),
            kind="verified_delivery",
            payload={
                "execution_kind": "native",
                "workspace": {
                    "repository_id": 123,
                    "repository": "example/factory",
                    "commit_sha": "c" * 40,
                    "base_revision": "a" * 40,
                },
                "delivery": {
                    "mode": "pull_request",
                    "pull_request": {"number": 3, "url": url},
                },
            },
        )
        return {
            "schema_version": 1,
            "status": "awaiting_review",
            "pull_number": 3,
            "repository_id": 123,
            "head_sha": "c" * 40,
            "base_sha": "a" * 40,
            "observed_at": timezone.now().isoformat(),
        }

    def test_detail_renders_saved_roles_identity_and_unavailable_usage(self):
        self.build()
        self.routing()
        self.checks()
        self.review(
            "reviewer-security",
            "changes",
            [
                {
                    "id": "SEC-1",
                    "summary": "Check query handling",
                    "evidence": ["work_queue/views.py:42"],
                    "criterion_ids": ["criterion-1"],
                }
            ],
        )
        response = self.detail()
        for text in [
            "Factory rounds and evidence",
            "Code review",
            "Security review",
            "missing",
            "changes",
            "SEC-1",
            "fixture-model",
            "2.50 seconds",
            "Usage and billed cost unavailable",
            "Current evidence incomplete",
        ]:
            self.assertContains(response, text)
        self.assertNotContains(response, 'status-succeeded">Ready for human review')

    def test_activity_and_history_render_real_related_artifacts(self):
        self.complete_round()
        activity = self.client.get(reverse("runner_activity"))
        self.assertContains(activity, "Ready for human review")
        Run.objects.filter(pk=self.run.pk).update(
            state="completed",
            active_slot=None,
            result_status="succeeded",
            native_outcome="delivered",
            result_url="https://github.com/example/factory/pull/3",
        )
        history = self.client.get(reverse("runner_plan_history"))
        self.assertContains(history, "ready for human review")
        self.assertContains(history, "Merge not yet observed")
        self.assertContains(self.detail(), "worker capacity is released")

    def test_current_specification_and_intake_are_linked_input_evidence(self):
        self.complete_round()
        response = self.detail()
        self.assertContains(response, "Current linked specification input")
        self.assertContains(response, "Current linked intake input")
        self.assertNotContains(response, "Earlier candidate or specification")

    def test_old_tree_reviews_do_not_satisfy_current_round(self):
        self.complete_round()
        self.build(round=2, tree="d" * 40)
        self.routing(round=2, tree="d" * 40)
        self.checks(round=2, tree="d" * 40)
        response = self.detail()
        self.assertFalse(response.context["run"].factory["ready"])
        self.assertEqual(
            [
                review["verdict"]
                for review in response.context["run"].factory["reviewers"]
            ],
            ["missing", "missing"],
        )
        self.assertContains(response, "Earlier candidate or specification")

    def test_ready_decision_without_current_artifact_references_is_incomplete(self):
        self.build()
        self.routing()
        self.checks()
        self.review()
        self.review("reviewer-security")
        self.artifact(
            "decision",
            {
                "verdict": "ready",
                "rationale": "Unreferenced assertion",
                "artifact_ids": [str(uuid4())],
            },
        )
        self.assertFalse(self.detail().context["run"].factory["ready"])

    def test_ready_decision_without_criterion_coverage_is_incomplete(self):
        self.build()
        entries = [
            self.checks(),
            self.routing(),
            self.review(coverage=[]),
            self.review("reviewer-security"),
        ]
        self.artifact(
            "decision",
            {
                "verdict": "ready",
                "rationale": "Model asserted readiness without coverage",
                "artifact_ids": [str(entry.pk) for entry in entries],
            },
        )
        self.assertFalse(self.detail().context["run"].factory["ready"])

    def test_missing_current_references_never_show_a_green_review(self):
        self.complete_round()
        Run.objects.filter(pk=self.run.pk).update(plan_reference="")
        response = self.detail()
        self.assertFalse(response.context["run"].factory["ready"])
        self.assertNotContains(response, 'status-succeeded">pass')

    def test_ready_decision_without_current_accepted_intake_is_incomplete(self):
        self.complete_round()
        self.artifact(
            "intake",
            {
                "verdict": "reject",
                "scope": "Changed scope",
                "rationale": "Cannot accept current task",
                "risk": "medium",
                "surfaces": ["security"],
                "questions": [],
            },
            round=2,
        )
        self.assertFalse(self.detail().context["run"].factory["ready"])

    def test_ready_decision_without_changed_path_attestation_is_incomplete(self):
        self.complete_round()
        workspace = (
            RunEvidence.objects.filter(run=self.run, kind="native_workspace")
            .last()
            .payload
        )
        RunEvidence.objects.create(
            run=self.run,
            operation_id=uuid4(),
            kind="native_workspace",
            payload={
                key: value for key, value in workspace.items() if key != "factory_paths"
            },
        )
        self.assertFalse(self.detail().context["run"].factory["ready"])

    def test_trusted_ui_paths_cannot_reuse_a_code_security_route(self):
        self.complete_round()
        workspace = (
            RunEvidence.objects.filter(run=self.run, kind="native_workspace")
            .last()
            .payload
        )
        RunEvidence.objects.create(
            run=self.run,
            operation_id=uuid4(),
            kind="native_workspace",
            payload={
                **workspace,
                "factory_paths": ["work_queue/templates/work_queue/index.html"],
            },
        )
        response = self.detail()
        self.assertFalse(response.context["run"].factory["ready"])
        self.assertNotContains(response, 'status-succeeded">pass')

    def test_new_specification_does_not_reuse_old_tree_reviews(self):
        self.complete_round()
        plan = PlanVersion.objects.create(
            run=self.run,
            context=self.context,
            operation_id=uuid4(),
            version=2,
            digest="d" * 64,
            document=self.plan.document,
            reason="Changed specification",
        )
        Run.objects.filter(pk=self.run.pk).update(plan_reference=str(plan.pk))
        response = self.detail()
        self.assertFalse(response.context["run"].factory["ready"])
        self.assertNotContains(response, 'status-succeeded">pass')

    def test_later_failed_validation_does_not_reuse_a_passing_receipt(self):
        self.complete_round()
        RunEvidence.objects.create(
            run=self.run,
            operation_id=uuid4(),
            kind="native_validation",
            payload={
                "state": "failed",
                "source_digest": "4" * 64,
                "tree_sha": "b" * 40,
            },
        )
        self.assertFalse(self.detail().context["run"].factory["ready"])

    def test_history_paginates_runs(self):
        for index in range(26):
            Run.objects.create(
                runner=self.runner,
                item=self.item,
                state="completed",
                configuration_snapshot={},
            )
        response = self.client.get(reverse("runner_plan_history"), {"page": 2})
        self.assertEqual(len(response.context["page"].object_list), 2)
        self.assertContains(response, "Page 2 of 2")

    def test_task_findings_are_escaped_without_active_links(self):
        self.build()
        self.review(
            findings=[
                {
                    "id": "XSS-1",
                    "summary": "<img src=x onerror=alert(1)>",
                    "evidence": ["javascript:alert(1)"],
                    "criterion_ids": [],
                }
            ]
        )
        response = self.detail()
        self.assertContains(response, "&lt;img")
        self.assertNotContains(response, "<img src=x")
        self.assertNotContains(response, 'href="javascript:')

    @override_settings(NQ_CREDENTIAL_KEY=TEST_KEY)
    def test_artifact_findings_redact_selected_credentials_and_claim_tokens(self):
        credential = Credential(name="UI redaction", provider="github", kind="token")
        credential.set_secret("selected-factory-secret")
        credential.save()
        claim = uuid4()
        QueueItem.objects.filter(pk=self.item.pk).update(claim_token=claim)
        Run.objects.filter(pk=self.run.pk).update(
            configuration_snapshot={
                "execution_kind": "native",
                "effective": {"implementation_credential_id": credential.pk},
            }
        )
        self.build()
        self.review(
            findings=[
                {
                    "id": "secret",
                    "summary": f"selected-factory-secret {claim}",
                    "evidence": ["Selected backend output"],
                    "criterion_ids": [],
                }
            ]
        )
        for response in [
            self.detail(),
            self.client.get(reverse("runner_activity")),
            self.client.get(reverse("runner_plan_history")),
        ]:
            self.assertNotContains(response, "selected-factory-secret")
            self.assertNotContains(response, str(claim))

    def test_redaction_failure_hides_artifacts_and_current_readiness(self):
        self.complete_round()
        with patch(
            "runner_control.plan_presentation.redact_run_payload",
            side_effect=ValidationError("Unavailable"),
        ):
            response = self.detail()
        self.assertContains(response, "saved content is hidden")
        self.assertNotContains(response, "fixture-model")
        self.assertFalse(response.context["run"].factory["ready"])

    def test_detail_paginates_artifacts_and_bounds_payload_display(self):
        for round in range(1, 13):
            self.artifact(
                "build",
                {
                    "summary": "Bounded summary",
                    "notes": "x" * 20000,
                    "criteria": ["criterion-1"],
                },
                round=round,
            )
        response = self.detail(factory_page=2)
        self.assertEqual(len(response.context["factory_artifacts"]), 4)
        self.assertContains(response, "Artifact page 2 of 2")
        self.assertContains(response, "display limited to 16,000 characters")

    def test_summary_loads_current_authority_in_four_bounded_queries(self):
        self.complete_round()
        for _ in range(15):
            Run.objects.create(
                runner=self.runner,
                item=self.item,
                state="completed",
                configuration_snapshot={"execution_kind": "native"},
            )
        runs = list(Run.objects.select_related("item", "runner"))
        with CaptureQueriesContext(connection) as captured:
            prepared = prepare_factory_runs(runs)
        self.assertEqual(len(captured), 4)
        self.assertLessEqual(
            sum(len(run._factory_documents) for run in prepared), len(runs) * 110
        )

    def test_invalid_saved_document_is_not_displayed_as_evidence(self):
        self.artifact(
            "build",
            {"summary": "Unsupported sensitive content", "criteria": ["criterion-1"]},
            schema_version=99,
        )
        response = self.detail()
        self.assertContains(response, "unavailable or redacted")
        self.assertNotContains(response, "Unsupported sensitive content")

    def test_nonreview_findings_extension_does_not_break_saved_evidence(self):
        self.artifact(
            "build",
            {
                "summary": "Valid extension",
                "criteria": ["criterion-1"],
                "findings": None,
            },
        )
        self.assertContains(self.detail(), "Valid extension")
        self.assertEqual(self.client.get(reverse("runner_activity")).status_code, 200)

    def test_malformed_legacy_workspace_is_incomplete(self):
        self.complete_round()
        RunEvidence.objects.create(
            run=self.run,
            operation_id=uuid4(),
            kind="native_workspace",
            payload={"workspace": "legacy unknown"},
        )
        self.assertFalse(self.detail().context["run"].factory["ready"])

    def test_verified_publication_observation_is_separate_from_delivery(self):
        self.complete_round()
        payload = self.delivered_publication()
        RunEvidence.objects.create(
            run=self.run,
            operation_id=uuid4(),
            kind="publication_observation",
            payload=payload,
        )
        self.assertContains(self.detail(), "Awaiting human review")
        RunEvidence.objects.create(
            run=self.run,
            operation_id=uuid4(),
            kind="publication_observation",
            payload={**payload, "status": "closed_unmerged"},
        )
        self.assertContains(self.detail(), "Closed without merge")
        self.assertContains(self.detail(), "Factory evidence complete")
        self.assertNotContains(self.detail(), "Human review and merge are required")

    def test_merge_assertion_without_human_and_ancestry_proof_stays_unknown(self):
        payload = self.delivered_publication()
        RunEvidence.objects.create(
            run=self.run,
            operation_id=uuid4(),
            kind="publication_observation",
            payload={**payload, "status": "merged", "merge_sha": "e" * 40},
        )
        response = self.detail()
        self.assertContains(response, "Merge not yet observed")
        self.assertNotContains(response, "Human merge verified")

    def test_blocked_observation_cursor_does_not_claim_a_merge(self):
        self.delivered_publication()
        Run.objects.filter(pk=self.run.pk).update(
            native_status={
                "publication_observation": {
                    "status": "blocked",
                    "error": "Current authorization unavailable",
                }
            }
        )
        response = self.detail()
        self.assertContains(response, "Observation unavailable; merge not verified")
        self.assertNotContains(response, "Human merge verified")

    def test_human_merge_observation_requires_matching_delivery_and_ancestry(self):
        payload = self.delivered_publication()
        payload.update(
            status="merged",
            merge_sha="e" * 40,
            merged_at=timezone.now().isoformat(),
            merged_by={"id": 42, "type": "User", "login": "reviewer"},
            ancestry_proof={
                "publication_base": {
                    "base_sha": "a" * 40,
                    "head_sha": "e" * 40,
                    "status": "ahead",
                },
                "current_base": {
                    "base_sha": "e" * 40,
                    "head_sha": "f" * 40,
                    "status": "ahead",
                },
            },
        )
        RunEvidence.objects.create(
            run=self.run,
            operation_id=uuid4(),
            kind="publication_observation",
            payload=payload,
        )
        self.assertContains(self.detail(), "Human merge verified")
        RunEvidence.objects.create(
            run=self.run,
            operation_id=uuid4(),
            kind="publication_observation",
            payload={**payload, "head_sha": "d" * 40},
        )
        response = self.detail()
        self.assertContains(response, "Merge not yet observed")
        self.assertNotContains(response, "Human merge verified")

    def test_reading_factory_pages_never_changes_runs_or_artifacts(self):
        self.complete_round()
        before = list(Run.objects.values())
        artifacts = list(FactoryArtifact.objects.values())
        self.detail()
        self.client.get(reverse("runner_activity"))
        self.client.get(reverse("runner_plan_history"))
        self.assertEqual(before, list(Run.objects.values()))
        self.assertEqual(artifacts, list(FactoryArtifact.objects.values()))
