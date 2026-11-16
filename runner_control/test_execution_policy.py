import json
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.test import override_settings
from django.urls import reverse

from coding_runner.policy import ExecutionPolicy, default_execution_policy
from credentials.test_models import TEST_KEY
from work_queue.services import enqueue_item, requeue_items

from .configuration import configuration_document
from .approvals import decide_approval
from .execution_policy import current_native_policy
from .models import ActionApproval, RunnerIdentity
from .auth import token_digest
from .services import RunConflict, claim_run, record_evidence
from .settings_models import ProjectRunnerSettings, RunnerSettings
from .settings_forms import ProjectRunnerSettingsForm
from .test_settings import ConfiguredProjectTestCase
from .test_support import provision_controller


class NativeConfigurationTests(ConfiguredProjectTestCase):
    def test_native_configuration_does_not_require_research(self):
        saved = RunnerSettings.objects.get(pk=1)
        saved.research_backend = ""
        saved.research_model = ""
        saved.research_credential = None
        saved.save()
        project = configuration_document()["projects"][0]
        self.assertTrue(project["structurally_valid"], project["errors"])
        self.assertEqual(project["execution_kind"], "native")
        self.assertNotIn("command_approval", project["required_capabilities"])
        self.assertNotIn("backend:", project["required_capabilities"])
        self.assertEqual(project["delivery_contract_version"], 2)

    def test_unused_research_account_does_not_gate_native_execution(self):
        saved = RunnerSettings.objects.get(pk=1)
        saved.research_credential_id = None
        saved.research_backend = "claude"
        saved.research_model = "claude-sonnet-5-5"
        saved.save()
        document = configuration_document()
        self.assertEqual(len(document["accounts"]), 1)
        self.assertEqual(document["accounts"][0]["backend"], "codex")
        self.assertTrue(document["projects"][0]["structurally_valid"])

    def test_unused_research_timeout_does_not_limit_native_task_settings(self):
        settings = RunnerSettings(
            task_timeout_seconds=600,
            implementation_timeout_seconds=300,
            validation_timeout_seconds=300,
            research_timeout_seconds=1800,
        )
        settings.full_clean(validate_unique=False)

    def test_policy_override_is_bound_by_digest(self):
        policy = default_execution_policy()
        policy["cpu_limit"] = 4
        ProjectRunnerSettings.objects.create(
            project=self.project,
            overrides={"execution_policy": {"mode": "replace", "value": policy}},
        )
        project = configuration_document()["projects"][0]
        self.assertEqual(project["execution_policy"], policy)
        self.assertEqual(
            project["policy_digest"], ExecutionPolicy.from_data(policy).digest
        )

    def test_policy_cannot_be_cleared(self):
        form = ProjectRunnerSettingsForm({"execution_policy_mode": "clear"})
        self.assertFalse(form.is_valid())
        self.assertIn("cannot be cleared", str(form.errors))

    def test_unknown_capability_cannot_be_added_to_policy(self):
        policy = default_execution_policy()
        policy["host_access"] = True
        with self.assertRaises(ValidationError):
            RunnerSettings(execution_policy=policy).full_clean()

    def test_policy_version_changes_configuration_revision(self):
        before = configuration_document()
        saved = RunnerSettings.objects.get(pk=1)
        saved.execution_policy["repair_limit"] = 1
        saved.save()
        after = configuration_document()
        self.assertNotEqual(before["revision"], after["revision"])
        self.assertNotEqual(
            before["projects"][0]["policy_digest"],
            after["projects"][0]["policy_digest"],
        )

    def test_native_runtime_is_not_claimed_ready_by_contracts(self):
        document = configuration_document()
        self.assertFalse(document["execution_ready"])
        self.assertFalse(document["release_verification"]["qualified"])
        self.assertEqual(document["delivery_contract"]["schema_version"], 2)


@override_settings(NQ_CREDENTIAL_KEY=TEST_KEY)
class NativePolicyOwnershipTests(ConfiguredProjectTestCase):
    def setUp(self):
        super().setUp()
        self.github.set_secret("native-test-github-token")
        type(self.github).objects.filter(pk=self.github.pk).update(
            encrypted_payload=self.github.encrypted_payload
        )
        self.backend.set_secret("native-test-backend-token")
        type(self.backend).objects.filter(pk=self.backend.pk).update(
            encrypted_payload=self.backend.encrypted_payload
        )
        self.token = "native-policy-runner-token"
        self.runner = RunnerIdentity.objects.create(
            name="native-policy", token_digest=token_digest(self.token)
        )
        self.client.defaults["HTTP_AUTHORIZATION"] = "Bearer " + self.token
        provision_controller(self)
        saved = RunnerSettings.objects.get(pk=1)
        saved.enabled = True
        saved.save()
        self.item = enqueue_item(project=self.project, title="Native work")
        self.run = claim_run(
            runner=self.runner,
            run_id=uuid4(),
            configuration_revision=configuration_document()["revision"],
        )
        self.item.refresh_from_db()

    def test_current_policy_uses_owned_snapshot(self):
        policy = current_native_policy(self.run, self.item.claim_token)
        self.assertEqual(
            policy.digest, self.run.configuration_snapshot["policy_digest"]
        )

    def test_changed_policy_cannot_authorize_execution(self):
        saved = RunnerSettings.objects.get(pk=1)
        saved.execution_policy["network_hosts"] = []
        saved.save()
        with self.assertRaisesMessage(RunConflict, "policy changed"):
            current_native_policy(self.run, self.item.claim_token)

    def test_stale_claim_cannot_authorize_execution(self):
        requeue_items(type(self.item).objects.filter(pk=self.item.pk))
        with self.assertRaises(RunConflict):
            current_native_policy(self.run, self.item.claim_token)

    def test_paused_policy_cannot_authorize_execution(self):
        saved = RunnerSettings.objects.get(pk=1)
        saved.paused = True
        saved.save()
        with self.assertRaisesMessage(RunConflict, "disabled or paused"):
            current_native_policy(self.run, self.item.claim_token)

    def test_native_run_rejects_approval_creation(self):
        response = self.client.post(
            reverse("runner_approvals", args=[self.run.pk]),
            json.dumps(
                {
                    "claim_token": str(self.item.claim_token),
                    "operation_id": str(uuid4()),
                    "action": {},
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 409)
        self.assertFalse(ActionApproval.objects.exists())

    def test_native_progress_does_not_require_legacy_plan(self):
        record_evidence(
            runner=self.runner,
            run_id=self.run.pk,
            claim_token=self.item.claim_token,
            operation_id=uuid4(),
            kind="native_progress",
            payload={},
            progress={"stage": "implementation"},
        )
        self.run.refresh_from_db()
        self.assertEqual(self.run.stage, "implementation")

    def test_existing_approved_action_cannot_dispatch_under_native_policy(self):
        approval = ActionApproval.objects.create(
            run=self.run,
            operation_id=uuid4(),
            action={},
            binding_digest="a" * 64,
            decision="approved",
        )
        response = self.client.post(
            reverse("runner_approval_consume", args=[self.run.pk, approval.pk]),
            json.dumps(
                {
                    "claim_token": str(self.item.claim_token),
                    "execution_id": str(uuid4()),
                    "binding_digest": approval.binding_digest,
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 409)
        approval.refresh_from_db()
        self.assertIsNone(approval.consumed_at)

    def test_native_request_cannot_receive_human_permission(self):
        approval = ActionApproval.objects.create(
            run=self.run,
            operation_id=uuid4(),
            action={},
            binding_digest="a" * 64,
        )
        with self.assertRaisesMessage(RunConflict, "project policy"):
            decide_approval(approval.pk, "approved", approval.binding_digest)
        approval.refresh_from_db()
        self.assertEqual(approval.decision, "pending")

    def test_native_delivery_cannot_use_legacy_success_contract(self):
        with self.assertRaisesMessage(
            ValidationError, "version 2 delivery authorization"
        ):
            record_evidence(
                runner=self.runner,
                run_id=self.run.pk,
                claim_token=self.item.claim_token,
                operation_id=uuid4(),
                kind="verified_delivery",
                payload={"schema_version": 1},
            )

    def test_native_pending_approval_has_no_decision_controls(self):
        ActionApproval.objects.create(
            run=self.run,
            operation_id=uuid4(),
            action={"argv": ["test"]},
            binding_digest="a" * 64,
        )
        response = self.client.get(reverse("runner_approval_inbox"))
        self.assertNotContains(response, "Approve this exact action once")
