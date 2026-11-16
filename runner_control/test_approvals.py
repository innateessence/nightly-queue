from .test_support import CONTROLLER_SECRET, provision_controller
import json
from uuid import uuid4
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from coding_runner.approvals import ActionSpec
from credentials.models import Credential
from credentials.test_models import TEST_KEY
from projects.models import Project
from work_queue.services import enqueue_item, requeue_items

from .approvals import decide_approval
from .auth import token_digest
from .models import ActionApproval, Run, RunnerIdentity
from .services import RunConflict


@override_settings(NQ_API_TOKEN="generic-test-token", NQ_CREDENTIAL_KEY=TEST_KEY)
class ApprovalTests(TestCase):
    def setUp(self):
        snapshot = patch(
            "runner_control.services.locked_configuration_snapshot",
            return_value=("", {}),
        )
        snapshot.start()
        self.addCleanup(snapshot.stop)
        self.token = "approval-runner-test-token"
        self.runner = RunnerIdentity.objects.create(
            controller_digest=token_digest(CONTROLLER_SECRET),
            name="approvals",
            token_digest=token_digest(self.token),
        )
        self.client.defaults["HTTP_AUTHORIZATION"] = "Bearer " + self.token
        provision_controller(self)
        project = Project.objects.create(
            name="Example", repository_url="https://github.com/example/repo"
        )
        self.item = enqueue_item(project=project, title="Untrusted task")
        self.run_id = uuid4()
        response = self.client.post(
            reverse("runner_claim"),
            json.dumps({"run_id": str(self.run_id)}),
            content_type="application/json",
        )
        self.claim = response.json()["item"]["claim_token"]
        self.operation_id = uuid4()
        self.action = {
            "argv": ["/usr/bin/printf", "%s", "$(touch /workspace/injected)"],
            "mode": "exec",
            "source_kind": "comment",
            "source_reference": "https://github.com/example/repo/issues/1#comment-1",
            "source_digest": "a" * 64,
            "workspace_id": str(self.run_id),
            "base_revision": "b" * 40,
            "content_digest": "c" * 64,
        }

    def request_approval(self, **overrides):
        data = {
            "claim_token": self.claim,
            "operation_id": str(self.operation_id),
            "action": self.action,
        }
        return self.client.post(
            reverse("runner_approvals", args=[self.run_id]),
            json.dumps({**data, **overrides}),
            content_type="application/json",
        )

    def approval(self):
        response = self.request_approval()
        self.assertEqual(response.status_code, 200)
        return ActionApproval.objects.get(pk=response.json()["approval"]["id"])

    def consume(self, approval, **overrides):
        data = {
            "claim_token": self.claim,
            "execution_id": str(uuid4()),
            "binding_digest": approval.binding_digest,
        }
        return self.client.post(
            reverse("runner_approval_consume", args=[self.run_id, approval.pk]),
            json.dumps({**data, **overrides}),
            content_type="application/json",
        )

    def approve(self, approval):
        decide_approval(approval.pk, "approved", approval.binding_digest)

    def selected_credential(self, secret, *, provider="codex", kind="token"):
        credential = Credential(name="Selected", provider=provider, kind=kind)
        credential.set_secret(secret)
        credential.save()
        snapshot = {"effective": {"research_credential_id": credential.pk}}
        Run.objects.filter(pk=self.run_id).update(configuration_snapshot=snapshot)
        return credential

    def legacy_approval(self, secret):
        approval = self.approval()
        approval.action["argv"].append(secret)
        approval.binding_digest = ActionSpec.from_data(approval.action).binding_digest
        approval.decision_note = secret
        approval.save()
        return approval

    def test_injected_command_is_only_pending_data(self):
        approval = self.approval()
        self.assertEqual(approval.action, self.action)
        self.assertEqual(approval.decision, "pending")
        self.assertIsNone(approval.execution_id)
        self.assertEqual(self.consume(approval).status_code, 409)
        run = Run.objects.get(pk=self.run_id)
        self.assertEqual(run.stage, "waiting")
        self.assertIn("Human approval", run.wait_reason)

    def test_duplicate_request_is_idempotent_after_restart(self):
        approval = self.approval()
        self.client = Client(
            HTTP_X_NQ_CONTROLLER_SECRET=CONTROLLER_SECRET,
            HTTP_AUTHORIZATION="Bearer " + self.token,
        )
        replay = self.request_approval()
        self.assertEqual(replay.json()["approval"]["id"], str(approval.pk))
        self.assertEqual(ActionApproval.objects.count(), 1)
        self.assertEqual(replay.json()["approval"]["decision"], "pending")

    def test_foreign_workspace_cannot_request_under_this_claim(self):
        action = {**self.action, "workspace_id": str(uuid4())}
        self.assertEqual(self.request_approval(action=action).status_code, 409)
        self.assertEqual(ActionApproval.objects.count(), 0)
        self.assertEqual(Run.objects.get(pk=self.run_id).stage, "claimed")

    def test_previously_saved_foreign_workspace_cannot_consume_permit(self):
        approval = self.approval()
        approval.action["workspace_id"] = str(uuid4())
        approval.binding_digest = ActionSpec.from_data(approval.action).binding_digest
        approval.decision = "approved"
        approval.save()
        self.assertEqual(self.consume(approval).status_code, 409)
        approval.refresh_from_db()
        self.assertIsNone(approval.consumed_at)

    def test_previously_saved_foreign_workspace_cannot_receive_decision(self):
        approval = self.approval()
        approval.action["workspace_id"] = str(uuid4())
        approval.binding_digest = ActionSpec.from_data(approval.action).binding_digest
        approval.save()
        with self.assertRaises(RunConflict):
            self.approve(approval)
        approval.refresh_from_db()
        self.assertEqual(approval.decision, "pending")

    def test_changed_action_requires_a_new_decision(self):
        approval = self.approval()
        self.approve(approval)
        changed = {**self.action, "argv": ["/usr/bin/touch", "/workspace/injected"]}
        self.assertEqual(self.request_approval(action=changed).status_code, 409)
        self.operation_id = uuid4()
        response = self.request_approval(action=changed)
        self.assertEqual(response.json()["approval"]["decision"], "pending")

    def test_changed_source_requires_a_new_decision(self):
        approval = self.approval()
        self.approve(approval)
        changed = {**self.action, "source_digest": "d" * 64}
        self.assertEqual(self.request_approval(action=changed).status_code, 409)
        self.assertEqual(
            self.consume(approval, binding_digest="d" * 64).status_code, 409
        )

    def test_denial_cannot_be_consumed(self):
        approval = self.approval()
        decide_approval(
            approval.pk, "denied", approval.binding_digest, "Unsafe command"
        )
        self.assertEqual(self.consume(approval).status_code, 409)
        approval.refresh_from_db()
        self.assertIsNone(approval.consumed_at)

    def test_approved_action_grants_one_permit_across_restarts(self):
        approval = self.approval()
        self.approve(approval)
        first = self.consume(approval)
        self.assertTrue(first.json()["permit"])
        self.client = Client(
            HTTP_X_NQ_CONTROLLER_SECRET=CONTROLLER_SECRET,
            HTTP_AUTHORIZATION="Bearer " + self.token,
        )
        second = self.consume(approval)
        self.assertFalse(second.json()["permit"])
        self.assertEqual(first.json()["execution_id"], second.json()["execution_id"])

    def test_tampering_with_stored_action_invalidates_approval(self):
        approval = self.approval()
        self.approve(approval)
        approval.action["argv"] = ["/usr/bin/false"]
        approval.save(update_fields=["action"])
        self.assertEqual(self.consume(approval).status_code, 409)

    def test_plan_and_operator_origins_also_require_human_decision(self):
        for kind in ["plan", "operator", "repository", "research", "issue"]:
            self.operation_id = uuid4()
            response = self.request_approval(
                action={**self.action, "source_kind": kind}
            )
            self.assertEqual(response.json()["approval"]["decision"], "pending")

    def test_api_cannot_submit_an_approval_decision(self):
        self.assertEqual(self.request_approval(decision="approved").status_code, 400)

    def test_known_credentials_are_rejected_without_storing_them(self):
        for secret in [self.token, self.claim]:
            response = self.request_approval(
                action={**self.action, "argv": ["/usr/bin/printf", secret]}
            )
            self.assertEqual(response.status_code, 400)
            self.assertNotIn(secret, response.content.decode())
        self.assertEqual(ActionApproval.objects.count(), 0)

    def test_escaped_bearer_is_rejected_as_actual_string(self):
        token = 'runner-"quote\\unicode-é'
        self.runner.token_digest = token_digest(token)
        self.runner.save()
        self.client.defaults["HTTP_AUTHORIZATION"] = "Bearer " + token
        action = {**self.action, "argv": ["/usr/bin/printf", token]}
        self.assertEqual(self.request_approval(action=action).status_code, 400)
        self.assertEqual(ActionApproval.objects.count(), 0)

    def test_selected_secret_in_argv_is_rejected_without_persistence(self):
        secret = 'selected-private-"quote\\unicode-é'
        self.selected_credential(secret)
        action = {**self.action, "argv": ["/usr/bin/printf", secret]}
        response = self.request_approval(action=action)
        self.assertEqual(response.status_code, 400)
        self.assertNotIn(secret, response.content.decode())
        self.assertEqual(ActionApproval.objects.count(), 0)

    def test_selected_secret_in_provenance_is_rejected(self):
        self.selected_credential("selected-private-secret")
        action = {**self.action, "source_reference": "selected-private-secret"}
        self.assertEqual(self.request_approval(action=action).status_code, 400)
        self.assertEqual(ActionApproval.objects.count(), 0)

    def test_rotated_selected_secret_is_still_rejected(self):
        credential = self.selected_credential("old-selected-private-secret")
        credential.set_secret("new-selected-private-secret")
        credential.save()
        action = {
            **self.action,
            "argv": ["/usr/bin/printf", "old-selected-private-secret"],
        }
        self.assertEqual(self.request_approval(action=action).status_code, 400)
        self.assertEqual(ActionApproval.objects.count(), 0)

    def test_nested_selected_connection_key_is_rejected(self):
        material = {"provider": "openai", "api_key": "nested-selected-private-key"}
        self.selected_credential(
            json.dumps(material), provider="opencode", kind="connection"
        )
        action = {**self.action, "argv": ["/usr/bin/printf", material["api_key"]]}
        self.assertEqual(self.request_approval(action=action).status_code, 400)

    def test_secret_validation_does_not_decrypt_unselected_credentials(self):
        self.selected_credential("selected-private-secret")
        Credential.objects.create(
            name="Unselected",
            provider="codex",
            kind="token",
            encrypted_payload="invalid",
        )
        self.assertEqual(self.request_approval().status_code, 200)

    def test_foreign_run_is_rejected_before_selected_secret_decryption(self):
        credential = self.selected_credential("selected-private-secret")
        Credential.objects.filter(pk=credential.pk).update(encrypted_payload="invalid")
        RunnerIdentity.objects.create(
            name="foreign", token_digest=token_digest("foreign-token")
        )
        self.client.defaults["HTTP_AUTHORIZATION"] = "Bearer foreign-token"
        self.assertEqual(self.request_approval().status_code, 404)

    def test_stale_claim_is_rejected_before_selected_secret_decryption(self):
        credential = self.selected_credential("selected-private-secret")
        Credential.objects.filter(pk=credential.pk).update(encrypted_payload="invalid")
        requeue_items(type(self.item).objects.filter(pk=self.item.pk))
        self.assertEqual(self.request_approval().status_code, 409)

    def test_missing_encryption_key_blocks_requests_without_persistence(self):
        self.selected_credential("selected-private-secret")
        with override_settings(NQ_CREDENTIAL_KEY=""):
            self.assertEqual(self.request_approval().status_code, 400)
        self.assertEqual(ActionApproval.objects.count(), 0)

    def test_legacy_readbacks_redact_selected_secrets_without_changing_binding(self):
        secret = "selected-private-secret"
        self.selected_credential(secret)
        approval = self.legacy_approval(secret)
        urls = [
            reverse("runner_approvals", args=[self.run_id]),
            reverse("runner_approval", args=[self.run_id, approval.pk]),
            reverse("runner_approval_inbox"),
        ]
        for url in urls:
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)
                self.assertNotIn(secret, response.content.decode())
        self.assertNotContains(response, "Approve this exact action once")
        approval.refresh_from_db()
        self.assertIn(secret, approval.action["argv"])
        self.assertEqual(
            approval.binding_digest,
            ActionSpec.from_data(approval.action).binding_digest,
        )
        self.assertEqual(approval.decision_note, secret)

    def test_missing_key_blocks_legacy_api_and_ui_displays(self):
        self.selected_credential("selected-private-secret")
        approval = self.legacy_approval("selected-private-secret")
        with override_settings(NQ_CREDENTIAL_KEY=""):
            detail = self.client.get(
                reverse("runner_approval", args=[self.run_id, approval.pk])
            )
            inbox = self.client.get(reverse("runner_approval_inbox"))
        self.assertEqual(detail.status_code, 400)
        self.assertEqual(inbox.status_code, 503)
        self.assertNotIn("selected-private-secret", detail.content.decode())
        self.assertNotIn("selected-private-secret", inbox.content.decode())

    def test_selected_secret_in_legacy_action_prevents_decision(self):
        self.selected_credential("selected-private-secret")
        approval = self.legacy_approval("selected-private-secret")
        with self.assertRaises(ValidationError):
            self.approve(approval)
        approval.refresh_from_db()
        self.assertEqual(approval.decision, "pending")

    def test_inbox_redacts_selected_secret_from_task_title(self):
        self.selected_credential("selected-private-secret")
        self.approval()
        self.item.title = "Task selected-private-secret"
        self.item.save()
        response = self.client.get(reverse("runner_approval_inbox"))
        self.assertNotContains(response, "selected-private-secret")
        self.assertContains(response, "Task [redacted]")
        self.item.refresh_from_db()
        self.assertEqual(self.item.title, "Task selected-private-secret")

    def test_inbox_redacts_selected_secret_from_binding_metadata(self):
        approval = self.approval()
        self.selected_credential(approval.binding_digest)
        response = self.client.get(reverse("runner_approval_inbox"))
        self.assertNotContains(response, approval.binding_digest)
        self.assertNotContains(response, "Approve this exact action once")
        approval.refresh_from_db()
        self.assertEqual(
            approval.binding_digest,
            ActionSpec.from_data(approval.action).binding_digest,
        )

    def test_selected_secret_in_legacy_action_prevents_consumption(self):
        self.selected_credential("selected-private-secret")
        approval = self.legacy_approval("selected-private-secret")
        approval.decision = "approved"
        approval.save()
        self.assertEqual(self.consume(approval).status_code, 400)
        approval.refresh_from_db()
        self.assertIsNone(approval.consumed_at)

    def test_decision_note_redacts_selected_secret(self):
        self.selected_credential("selected-private-secret")
        approval = self.approval()
        decide_approval(
            approval.pk,
            "approved",
            approval.binding_digest,
            "used selected-private-secret",
        )
        approval.refresh_from_db()
        self.assertEqual(approval.decision_note, "used [redacted]")

    def test_stale_claim_after_requeue_cannot_consume_approval(self):
        approval = self.approval()
        self.approve(approval)
        requeue_items(type(self.item).objects.filter(pk=self.item.pk))
        self.assertEqual(self.consume(approval).status_code, 409)

    def test_foreign_runner_cannot_read_request_or_consume(self):
        approval = self.approval()
        RunnerIdentity.objects.create(
            controller_digest=token_digest(CONTROLLER_SECRET),
            name="foreign",
            token_digest=token_digest("foreign-token"),
        )
        self.client.defaults["HTTP_AUTHORIZATION"] = "Bearer foreign-token"
        self.assertEqual(
            self.client.get(
                reverse("runner_approval", args=[self.run_id, approval.pk])
            ).status_code,
            404,
        )
        self.assertEqual(self.request_approval().status_code, 404)
        self.assertEqual(self.consume(approval).status_code, 404)

    def test_ui_decision_requires_csrf_and_matching_display_binding(self):
        approval = self.approval()
        browser = Client(enforce_csrf_checks=True)
        url = reverse("runner_approval_decide", args=[approval.pk])
        response = browser.post(
            url, {"decision": "approved", "binding_digest": approval.binding_digest}
        )
        self.assertEqual(response.status_code, 403)
        inbox = browser.get(reverse("runner_approval_inbox"))
        self.assertContains(inbox, "Approve this exact action once")
        self.assertEqual(inbox["Cache-Control"], "no-store")
        csrf = browser.cookies["csrftoken"].value
        response = browser.post(
            url,
            {"decision": "approved", "binding_digest": "f" * 64},
            HTTP_X_CSRFTOKEN=csrf,
        )
        self.assertEqual(response.status_code, 409)
        response = browser.post(
            url,
            {"decision": "approved", "binding_digest": approval.binding_digest},
            HTTP_X_CSRFTOKEN=csrf,
        )
        self.assertEqual(response.status_code, 302)
        approval.refresh_from_db()
        self.assertEqual(approval.decision, "approved")

    def test_ui_escapes_untrusted_action_and_provenance(self):
        self.request_approval(
            action={**self.action, "source_reference": "<script>alert(1)</script>"}
        )
        response = self.client.get(reverse("runner_approval_inbox"))
        self.assertNotContains(response, "<script>alert(1)</script>")
        self.assertContains(response, "&lt;script&gt;alert(1)&lt;/script&gt;")

    def test_decision_is_immutable_and_cannot_be_replaced_by_get(self):
        approval = self.approval()
        self.approve(approval)
        with self.assertRaises(RunConflict):
            decide_approval(approval.pk, "denied", approval.binding_digest)
        self.assertEqual(
            self.client.get(
                reverse("runner_approval_decide", args=[approval.pk])
            ).status_code,
            405,
        )

    def test_action_request_is_bounded_and_unknown_fields_are_rejected(self):
        response = self.request_approval(
            action={**self.action, "argv": ["/usr/bin/printf", "a" * 33000]}
        )
        self.assertEqual(response.status_code, 400)
        response = self.request_approval(action={**self.action, "auto_approve": True})
        self.assertEqual(response.status_code, 400)

    def test_generic_token_cannot_read_approvals(self):
        self.client.defaults["HTTP_AUTHORIZATION"] = "Bearer generic-test-token"
        response = self.client.get(reverse("runner_approvals", args=[self.run_id]))
        self.assertEqual(response.status_code, 401)
