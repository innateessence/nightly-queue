import copy
import uuid

from django.core.exceptions import ValidationError
from coding_runner.context import digest
from coding_runner.approvals import ActionSpec

from runner_control.models import ActionApproval, Run, RunEvidence
from django.test import TestCase
from django.utils import timezone
from runner_control import test_plans

from .delivery import validate_delivery
from .services import finish_run


class DeliveryAPITests(TestCase):
    post = test_plans.ContextPlanAPITests.post
    save_context = test_plans.ContextPlanAPITests.save_context
    save_plan = test_plans.ContextPlanAPITests.save_plan
    handoff = test_plans.ContextPlanAPITests.handoff
    valid_handoff = test_plans.ContextPlanAPITests.valid_handoff

    def setUp(self):
        test_plans.ContextPlanAPITests.setUp(self)
        self.plan, self.context = self.valid_handoff()
        self.workspace = {
            "workspace_id": str(self.run_id),
            "base_revision": self.context["inspected_base_commit"],
            "branch": self.claimed["branch_name"],
            "head_revision": self.context["inspected_base_commit"],
            "content_digest": "c" * 64,
            "diff_digest": "d" * 64,
            "changed": "yes",
        }
        action = self.plan["document"]["validation_actions"][0]
        self.action = {
            **action,
            "workspace_id": str(self.run_id),
            "content_digest": "c" * 64,
        }
        self.execution_id = uuid.uuid4()
        ActionApproval.objects.create(
            run_id=self.run_id,
            operation_id=uuid.uuid4(),
            action=self.action,
            binding_digest=ActionSpec.from_data(self.action).binding_digest,
            decision="approved",
            execution_id=self.execution_id,
            consumed_at=timezone.now(),
        )
        check = {
            "phase": "validation",
            "action": self.action,
            "execution_id": str(self.execution_id),
            "exit_code": 0,
            "interrupted": False,
            "before": self.workspace,
            "after": self.workspace,
            "output_digest": "e" * 64,
            "output_bytes": 12,
        }
        evidence = {
            "schema_version": 1,
            "plan_id": self.plan["id"],
            "plan_digest": self.plan["digest"],
            "context_id": self.context["id"],
            "workspace": self.workspace,
            "validation": [check],
            "amendments": [],
        }
        self.pull = {
            "number": 23,
            "url": "https://github.com/example/repo/pull/23",
            "repository": "example/repo",
            "head": self.claimed["branch_name"],
            "base": "main",
            "commit_sha": "f" * 40,
        }
        self.payload = {
            "schema_version": 1,
            "implementation": evidence,
            "repository": "example/repo",
            "branch": self.claimed["branch_name"],
            "base_branch": "main",
            "base_revision": self.context["inspected_base_commit"],
            "commit_sha": "f" * 40,
            "tree_sha": "b" * 40,
            "pushed_revision": "f" * 40,
            "pull_request": self.pull,
        }

    def upload(self, payload=None, operation_id=None):
        return self.post(
            "runner_artifact",
            {
                "kind": "verified_delivery",
                "data": self.payload if payload is None else payload,
                "operation_id": str(operation_id or uuid.uuid4()),
            },
        )

    def finish_success(self):
        return self.client.post(
            f"/api/v1/runner/runs/{self.run_id}/finish/",
            {
                "claim_token": self.claim_token,
                "status": "succeeded",
                "result_url": self.pull["url"],
            },
            content_type="application/json",
        )

    def test_verified_delivery_allows_idempotent_owned_success(self):
        self.assertEqual(self.upload().status_code, 200)
        self.assertEqual(self.finish_success().status_code, 200)
        self.assertEqual(self.finish_success().status_code, 200)
        self.assertEqual(Run.objects.get(pk=self.run_id).result_url, self.pull["url"])

    def test_delivery_upload_is_idempotent(self):
        operation = uuid.uuid4()
        self.assertEqual(
            self.upload(operation_id=operation).json(),
            self.upload(operation_id=operation).json(),
        )
        self.assertEqual(
            RunEvidence.objects.filter(kind="verified_delivery").count(), 1
        )

    def test_wrong_repository_is_rejected(self):
        self.payload["pull_request"]["repository"] = "other/repo"
        self.assertEqual(self.upload().status_code, 400)

    def test_wrong_head_is_rejected(self):
        self.payload["pull_request"]["head"] = "another-task"
        self.assertEqual(self.upload().status_code, 400)

    def test_wrong_base_is_rejected(self):
        self.payload["base_branch"] = "other"
        self.assertEqual(self.upload().status_code, 400)

    def test_empty_pr_url_is_rejected(self):
        self.payload["pull_request"]["url"] = ""
        self.assertEqual(self.upload().status_code, 400)

    def test_failed_validation_is_rejected(self):
        self.payload["implementation"]["validation"][0]["exit_code"] = 1
        self.assertEqual(self.upload().status_code, 400)

    def test_bool_exit_code_is_rejected(self):
        self.payload["implementation"]["validation"][0]["exit_code"] = False
        self.assertEqual(self.upload().status_code, 400)

    def test_changed_diff_after_validation_is_rejected(self):
        self.payload["implementation"]["validation"][0]["after"] = {
            **self.workspace,
            "diff_digest": "9" * 64,
        }
        self.assertEqual(self.upload().status_code, 400)

    def test_changed_plan_after_delivery_prevents_finish(self):
        self.assertEqual(self.upload().status_code, 200)
        self.save_plan(self.context, reason="New validation needed")
        self.assertEqual(self.finish_success().status_code, 400)

    def test_forged_untyped_legacy_artifact_does_not_allow_success(self):
        self.post("runner_artifact", {"kind": "delivery", "data": {"verified": True}})
        self.assertEqual(self.finish_success().status_code, 400)

    def test_service_directly_checks_evidence(self):
        with self.assertRaises(ValidationError):
            finish_run(
                runner=self.runner,
                run_id=self.run_id,
                claim_token=self.claim_token,
                status="succeeded",
                result_url=self.pull["url"],
            )

    def test_bad_evidence_does_not_write_artifact(self):
        invalid = copy.deepcopy(self.payload)
        invalid["implementation"]["plan_digest"] = "e" * 64
        self.assertEqual(self.upload(invalid).status_code, 400)
        self.assertFalse(RunEvidence.objects.filter(kind="verified_delivery").exists())

    def test_configured_string_command_requires_matching_approved_evidence(self):
        run = Run.objects.get(pk=self.run_id)
        run.configuration_snapshot = {
            "effective": {"validation_commands": ["python -m unittest"]}
        }
        run.save()
        self.assertEqual(self.upload().status_code, 400)
        action = {
            **self.action,
            "argv": ["/bin/sh", "-c", "python -m unittest"],
            "mode": "shell",
            "source_kind": "operator",
            "source_reference": "run-configuration:" + str(self.run_id),
            "source_digest": digest(run.configuration_snapshot),
        }
        execution = uuid.uuid4()
        ActionApproval.objects.create(
            run_id=self.run_id,
            operation_id=uuid.uuid4(),
            action=action,
            binding_digest=ActionSpec.from_data(action).binding_digest,
            decision="approved",
            execution_id=execution,
            consumed_at=timezone.now(),
        )
        check = {
            **self.payload["implementation"]["validation"][0],
            "action": action,
            "execution_id": str(execution),
        }
        self.payload["implementation"]["validation"].append(check)
        self.assertEqual(self.upload().status_code, 200)
        self.assertEqual(self.finish_success().status_code, 200)

    def test_unconsumed_human_approval_cannot_assert_validation(self):
        ActionApproval.objects.filter(run_id=self.run_id).update(consumed_at=None)
        self.assertEqual(self.upload().status_code, 400)

    def test_foreign_execution_id_cannot_assert_validation(self):
        self.payload["implementation"]["validation"][0]["execution_id"] = str(
            uuid.uuid4()
        )
        self.assertEqual(self.upload().status_code, 400)

    def test_invalid_data_types_raise_validation_error(self):
        for payload in ({}, [], None, {**self.payload, "implementation": []}):
            with self.subTest(payload=payload):
                with self.assertRaises(ValidationError):
                    validate_delivery(Run.objects.get(pk=self.run_id), payload)
