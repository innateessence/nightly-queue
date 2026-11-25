from uuid import uuid4

from django.core.exceptions import ValidationError
from django.test import override_settings

from vibearr_runner.policy import ExecutionPolicy, default_execution_policy
from credentials.test_models import TEST_KEY
from work_queue.services import enqueue_item, requeue_items

from .configuration import configuration_document
from .execution_policy import current_native_policy
from .models import RunnerIdentity
from .auth import token_digest
from .services import RunConflict, claim_run, record_evidence
from .settings_models import ProjectRunnerSettings, RunnerSettings
from .settings_forms import ProjectRunnerSettingsForm
from .test_settings import ConfiguredProjectTestCase
from .test_support import provision_controller


class NativeConfigurationTests(ConfiguredProjectTestCase):

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


@override_settings(VIBEARR_CREDENTIAL_KEY=TEST_KEY)
class NativePolicyOwnershipTests(ConfiguredProjectTestCase):
    def setUp(self):
        super().setUp()
        self.github.set_secret("native-test-github-token")
        type(self.github).objects.filter(pk=self.github.pk).update(
            encrypted_payload=self.github.encrypted_payload
        )
        self.backend.set_secret("managed-subscription-login")
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

    def test_native_delivery_cannot_use_legacy_success_contract(self):
        with self.assertRaisesMessage(ValidationError, "Native delivery evidence"):
            record_evidence(
                runner=self.runner,
                run_id=self.run.pk,
                claim_token=self.item.claim_token,
                operation_id=uuid4(),
                kind="verified_delivery",
                payload={"schema_version": 1},
            )
