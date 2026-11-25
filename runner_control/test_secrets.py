from .test_support import CONTROLLER_SECRET, provision_controller
import json
import uuid

from cryptography.fernet import Fernet
from django.test import TestCase, override_settings
from django.db import transaction
from django.db.models.deletion import ProtectedError
from django.urls import reverse

from credentials.models import APISettings, Credential, RunnerPreflightGrant
from credentials.test_models import TEST_KEY
from projects.models import Project
from work_queue.services import enqueue_item, requeue_items

from .auth import token_digest
from .configuration import configuration_document, settings_values
from .models import Run, RunnerIdentity
from .services import claim_run
from .settings_models import ProjectRunnerSettings, RunnerSettings


@override_settings(VIBEARR_CREDENTIAL_KEY=TEST_KEY, VIBEARR_API_TOKEN="generic-token")
class SelectedSecretTests(TestCase):
    def setUp(self):
        self.runner = RunnerIdentity.objects.create(
            controller_digest=token_digest(CONTROLLER_SECRET),
            name="owner",
            token_digest=token_digest("owner-token"),
        )
        self.other = RunnerIdentity.objects.create(
            controller_digest=token_digest(CONTROLLER_SECRET),
            name="other",
            token_digest=token_digest("other-token"),
        )
        self.client.defaults["HTTP_AUTHORIZATION"] = "Bearer owner-token"
        provision_controller(self)
        self.login = self.credential(
            "Codex", "codex", "managed-subscription-login", "login"
        )
        self.issue = self.credential("Issue", "github", "issue-private-secret")
        self.repository = self.credential(
            "Repository", "github", "repository-private-secret"
        )
        self.unselected = self.credential(
            "Unselected", "github", "unselected-private-secret"
        )
        self.import_token = self.credential("Import", "github", "import-private-secret")
        APISettings.objects.create(github_credential=self.import_token)
        self.settings = RunnerSettings.objects.get(pk=1)
        self.settings.enabled = True
        self.settings.implementation_backend = "codex"
        self.settings.implementation_model = "test-model"
        self.settings.implementation_credential = self.login
        self.settings.issue_credential = self.issue
        self.settings.repository_credential = self.repository
        self.settings.validation_commands = ["python -m unittest"]
        self.settings.save()
        self.project = Project.objects.create(
            name="Example",
            repository_url="https://github.com/example/project",
            credential=self.import_token,
        )
        self.item = enqueue_item(project=self.project, title="Task")
        RunnerPreflightGrant.objects.create(
            runner=self.runner, credential=self.repository
        )
        self.url = reverse("runner_selected_secret")

    def credential(self, name, provider, secret, kind="token"):
        credential = Credential(name=name, provider=provider, kind=kind)
        credential.set_secret(secret)
        credential.save()
        return credential

    def post(self, data):
        response = self.client.post(
            self.url, json.dumps(data), content_type="application/json"
        )
        self.assertIn("no-store", response["Cache-Control"])
        return response

    def preflight(self, credential=None, backend="github"):
        data = {
            "scope": "preflight",
            "backend": backend,
            "credential_id": (credential or self.repository).pk,
        }
        if backend == "github":
            data["project_id"] = self.project.pk
        return self.post(data)

    def claim(self):
        self.run = claim_run(
            runner=self.runner,
            run_id=uuid.uuid4(),
            configuration_revision=configuration_document()["revision"],
        )
        return self.run

    def run_secret(self, purpose="repository", claim_token=None):
        return self.post(
            {
                "scope": "run",
                "run_id": str(self.run.pk),
                "claim_token": str(claim_token or self.run.item.claim_token),
                "purpose": purpose,
            }
        )

    def configure_separate_runner(self):
        values = settings_values(self.settings)
        fields = {field.attname for field in RunnerSettings._meta.concrete_fields}
        values = {key: value for key, value in values.items() if key in fields}
        values["repository_credential_id"] = self.unselected.pk
        return RunnerSettings.objects.create(
            name="Separate", identity=self.runner, **values
        )

    def test_assigned_runner_preflight_uses_its_own_account(self):
        self.configure_separate_runner()
        RunnerPreflightGrant.objects.create(
            runner=self.runner, credential=self.unselected
        )
        response = self.preflight(self.unselected)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()["material"]["secret"], "unselected-private-secret"
        )
        self.assertEqual(self.preflight(self.repository).status_code, 400)

    def test_assigned_run_receives_its_snapshotted_credential(self):
        self.configure_separate_runner()
        self.run = claim_run(
            runner=self.runner,
            run_id=uuid.uuid4(),
            configuration_revision=configuration_document(self.runner)["revision"],
        )
        response = self.run_secret()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()["material"]["secret"], "unselected-private-secret"
        )

    def test_additional_runner_names_redact_selected_material(self):
        saved = self.configure_separate_runner()
        saved.name = "Runner unselected-private-secret"
        saved.save()
        response = self.client.get(reverse("coding_runner_list"))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "unselected-private-secret")
        self.assertContains(response, "Runner [redacted]")

    def test_selected_preflight_returns_only_repository_material(self):
        response = self.preflight()
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(
            response.json()["material"],
            {"secret": "repository-private-secret"},
        )
        self.assertNotIn("issue-private-secret", response.content.decode())

    def test_selected_backend_preflight_delivers_only_login_reference(self):
        RunnerPreflightGrant.objects.create(runner=self.runner, credential=self.login)
        response = self.preflight(self.login, backend="codex")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(
            response.json()["material"], {"secret": "managed-subscription-login"}
        )
        self.assertNotIn("repository-private-secret", response.content.decode())

    def test_unselected_credential_rejected_even_with_grant(self):
        RunnerPreflightGrant.objects.create(
            runner=self.runner, credential=self.unselected
        )
        self.assertEqual(self.preflight(self.unselected).status_code, 400)

    def test_unrelated_runner_cannot_read_preflight(self):
        self.client.defaults["HTTP_AUTHORIZATION"] = "Bearer other-token"
        self.assertEqual(self.preflight().status_code, 400)

    def test_generic_queue_api_token_cannot_read_secrets(self):
        self.client.defaults["HTTP_AUTHORIZATION"] = "Bearer generic-token"
        self.assertEqual(self.preflight().status_code, 401)

    def test_missing_authentication_is_not_cacheable(self):
        self.client.defaults.clear()
        self.assertEqual(self.preflight().status_code, 401)

    def test_wrong_method_is_not_cacheable(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 405)
        self.assertIn("no-store", response["Cache-Control"])

    def test_owned_active_run_resolves_all_purposes(self):
        self.claim()
        expected = {
            "implementation": "managed-subscription-login",
            "issue": "issue-private-secret",
            "repository": "repository-private-secret",
        }
        for purpose, secret in expected.items():
            with self.subTest(purpose=purpose):
                response = self.run_secret(purpose)
                self.assertEqual(response.status_code, 200, response.content)
                self.assertEqual(response.json()["material"]["secret"], secret)

    def test_native_run_rejects_removed_research_purpose(self):
        self.claim()
        self.assertEqual(self.run_secret("research").status_code, 400)

    def test_foreign_run_returns_404(self):
        self.claim()
        self.client.defaults["HTTP_AUTHORIZATION"] = "Bearer other-token"
        self.assertEqual(self.run_secret().status_code, 404)

    def test_stale_token_is_rejected(self):
        self.claim()
        self.assertEqual(self.run_secret(claim_token=uuid.uuid4()).status_code, 409)

    def test_requeued_run_is_rejected(self):
        self.claim()
        requeue_items(self.item.__class__.objects.filter(pk=self.item.pk))
        self.assertEqual(self.run_secret().status_code, 409)

    def test_completed_run_is_rejected(self):
        self.claim()
        self.run.state = Run.State.COMPLETED
        self.run.active_slot = None
        self.run.save()
        self.assertEqual(self.run_secret().status_code, 409)

    def test_revoked_credential_rejected(self):
        self.repository.revoke()
        self.assertEqual(self.preflight().status_code, 400)
        codes = [
            entry["code"] for entry in configuration_document()["projects"][0]["errors"]
        ]
        self.assertIn("revoked_credential", codes)

    def test_active_run_deselection_is_rejected(self):
        self.claim()
        self.settings.repository_credential = self.unselected
        self.settings.save()
        self.assertEqual(self.run_secret().status_code, 400)

    def test_active_run_rotation_delivers_current_version(self):
        self.claim()
        before = self.run.configuration_snapshot.copy()
        self.repository.set_secret("rotated-private-secret")
        self.repository.save()
        response = self.run_secret()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["credential"]["version"], 2)
        self.assertEqual(
            response.json()["material"]["secret"], "rotated-private-secret"
        )
        self.run.refresh_from_db()
        self.assertEqual(self.run.configuration_snapshot, before)

    def test_issue_import_and_project_credential_do_not_fallback(self):
        self.claim()
        self.settings.issue_credential = None
        self.settings.save()
        response = self.run_secret("issue")
        self.assertEqual(response.status_code, 400)
        self.assertNotIn("import-private-secret", response.content.decode())

    def test_wrong_provider_is_rejected(self):
        RunnerSettings.objects.filter(pk=self.settings.pk).update(
            repository_credential=self.login
        )
        RunnerPreflightGrant.objects.create(runner=self.runner, credential=self.login)
        response = self.preflight(self.login)
        self.assertEqual(response.status_code, 400)
        self.assertIn("provider", response.content.decode())

    @override_settings(VIBEARR_CREDENTIAL_KEY="")
    def test_missing_encryption_key_returns_redacted_error(self):
        response = self.preflight()
        self.assertEqual(response.status_code, 400)
        self.assertIn("VIBEARR_CREDENTIAL_KEY", response.content.decode())
        self.assertNotIn("repository-private-secret", response.content.decode())

    def test_wrong_encryption_key_returns_redacted_error(self):
        with override_settings(VIBEARR_CREDENTIAL_KEY=Fernet.generate_key().decode()):
            response = self.preflight()
        self.assertEqual(response.status_code, 400)
        self.assertIn("decrypt", response.content.decode())
        self.assertNotIn(self.repository.encrypted_payload, response.content.decode())

    def test_project_override_account_is_deliverable_only_with_grant(self):
        ProjectRunnerSettings.objects.create(
            project=self.project,
            overrides={
                "repository_credential_id": {
                    "mode": "replace",
                    "value": self.unselected.pk,
                }
            },
        )
        self.assertEqual(self.preflight(self.unselected).status_code, 400)
        RunnerPreflightGrant.objects.create(
            runner=self.runner, credential=self.unselected
        )
        self.assertEqual(self.preflight(self.unselected).status_code, 200)

    def test_configuration_and_run_snapshot_never_include_secret(self):
        self.claim()
        document = json.dumps(configuration_document())
        snapshot = json.dumps(self.run.configuration_snapshot)
        for secret in (
            "managed-subscription-login",
            "issue-private-secret",
            "repository-private-secret",
            "import-private-secret",
        ):
            self.assertNotIn(secret, document)
            self.assertNotIn(secret, snapshot)
        self.assertEqual(
            configuration_document()["credentials"][0]["secret_version"], 1
        )

    def test_selected_repository_token_is_redacted_from_saved_plan(self):
        self.claim()
        response = self.client.post(
            reverse("runner_artifact", args=[self.run.pk]),
            json.dumps(
                {
                    "operation_id": str(uuid.uuid4()),
                    "claim_token": str(self.run.item.claim_token),
                    "kind": "plan",
                    "data": {"log": "backend failed using repository-private-secret"},
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertNotIn("repository-private-secret", response.content.decode())
        self.assertNotIn(
            "repository-private-secret", json.dumps(self.run.evidence.get().payload)
        )

    def test_issue_token_is_redacted_from_finish_error(self):
        self.issue.set_secret("nested-private-api-key")
        self.issue.save()
        self.claim()
        response = self.client.post(
            reverse("runner_finish", args=[self.run.pk]),
            json.dumps(
                {
                    "claim_token": str(self.run.item.claim_token),
                    "status": "failed",
                    "error_summary": "request failed with nested-private-api-key",
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertNotIn("nested-private-api-key", response.content.decode())
        self.run.refresh_from_db()
        self.item.refresh_from_db()
        self.assertNotIn("nested-private-api-key", self.run.error_summary)
        self.assertNotIn("nested-private-api-key", self.item.error_summary)

    def test_finish_retry_after_redaction_is_idempotent(self):
        self.claim()
        data = {
            "claim_token": str(self.run.item.claim_token),
            "status": "failed",
            "error_summary": "repository-private-secret failed",
        }
        url = reverse("runner_finish", args=[self.run.pk])
        first = self.client.post(url, json.dumps(data), content_type="application/json")
        self.repository.set_secret("rotated-private-secret")
        self.repository.save()
        second = self.client.post(
            url, json.dumps(data), content_type="application/json"
        )
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200, second.content)
        self.assertEqual(first.json(), second.json())

    def test_finish_retry_with_unavailable_key_does_not_decrypt(self):
        self.claim()
        data = {
            "claim_token": str(self.run.item.claim_token),
            "status": "failed",
            "error_summary": "repository-private-secret failed",
        }
        url = reverse("runner_finish", args=[self.run.pk])
        first = self.client.post(url, json.dumps(data), content_type="application/json")
        with override_settings(VIBEARR_CREDENTIAL_KEY=""):
            second = self.client.post(
                url, json.dumps(data), content_type="application/json"
            )
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200, second.content)
        self.assertEqual(first.json(), second.json())

    def test_old_delivered_secret_is_redacted_after_rotation(self):
        self.claim()
        self.assertEqual(self.run_secret().status_code, 200)
        self.repository.set_secret("rotated-private-secret")
        self.repository.save()
        response = self.client.post(
            reverse("runner_artifact", args=[self.run.pk]),
            json.dumps(
                {
                    "operation_id": str(uuid.uuid4()),
                    "claim_token": str(self.run.item.claim_token),
                    "kind": "plan",
                    "data": {
                        "log": "repository-private-secret and rotated-private-secret"
                    },
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(
            self.run.evidence.get().payload, {"log": "[redacted] and [redacted]"}
        )

    def test_artifact_retry_after_rotation_returns_same_evidence(self):
        self.claim()
        data = {
            "operation_id": str(uuid.uuid4()),
            "claim_token": str(self.run.item.claim_token),
            "kind": "plan",
            "data": {"log": "repository-private-secret failed"},
        }
        url = reverse("runner_artifact", args=[self.run.pk])
        first = self.client.post(url, json.dumps(data), content_type="application/json")
        self.repository.set_secret("rotated-private-secret")
        self.repository.save()
        second = self.client.post(
            url, json.dumps(data), content_type="application/json"
        )
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200, second.content)
        self.assertEqual(first.json(), second.json())
        self.assertEqual(self.run.evidence.count(), 1)

    def test_report_does_not_decrypt_unselected_credentials(self):
        Credential.objects.filter(pk=self.unselected.pk).update(
            encrypted_payload="invalid"
        )
        self.claim()
        response = self.client.post(
            reverse("runner_progress", args=[self.run.pk]),
            json.dumps(
                {
                    "operation_id": str(uuid.uuid4()),
                    "claim_token": str(self.run.item.claim_token),
                    "stage": "context",
                    "wait_reason": "repository-private-secret failed",
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.run.refresh_from_db()
        self.assertNotIn("repository-private-secret", self.run.wait_reason)
        self.assertNotIn("repository-private-secret", response.content.decode())

    def test_report_with_unavailable_encryption_fails_closed(self):
        self.claim()
        with override_settings(VIBEARR_CREDENTIAL_KEY=""):
            response = self.client.post(
                reverse("runner_artifact", args=[self.run.pk]),
                json.dumps(
                    {
                        "operation_id": str(uuid.uuid4()),
                        "claim_token": str(self.run.item.claim_token),
                        "kind": "plan",
                        "data": {"log": "repository-private-secret"},
                    }
                ),
                content_type="application/json",
            )
        self.assertEqual(response.status_code, 400)
        self.assertIn("VIBEARR_CREDENTIAL_KEY", response.content.decode())
        self.assertNotIn("repository-private-secret", response.content.decode())
        self.assertFalse(self.run.evidence.exists())

    def deselect_repository(self):
        RunnerPreflightGrant.objects.filter(credential=self.repository).delete()
        self.settings.repository_credential = self.unselected
        self.settings.save()

    @override_settings(VIBEARR_CREDENTIAL_KEY="")
    def test_snapshot_selection_protects_deselected_credential(self):
        self.claim()
        self.deselect_repository()
        with self.assertRaises(ProtectedError):
            self.repository.delete()

    def test_snapshot_selection_protects_bulk_credential_deletion(self):
        self.claim()
        self.deselect_repository()
        with self.assertRaises(ProtectedError):
            Credential.objects.filter(pk=self.repository.pk).delete()

    def test_completed_snapshot_protects_deselected_credential(self):
        self.claim()
        self.run.state = Run.State.COMPLETED
        self.run.active_slot = None
        self.run.save()
        self.deselect_repository()
        with self.assertRaises(ProtectedError):
            self.repository.delete()

    def test_obsolete_snapshot_protects_deselected_credential(self):
        self.claim()
        requeue_items(self.item.__class__.objects.filter(pk=self.item.pk))
        self.deselect_repository()
        with self.assertRaises(ProtectedError):
            self.repository.delete()

    def test_deselection_and_deletion_attempt_allow_failed_finish(self):
        self.claim()
        self.deselect_repository()
        try:
            with transaction.atomic():
                self.repository.delete()
        except ProtectedError:
            pass
        response = self.client.post(
            reverse("runner_finish", args=[self.run.pk]),
            json.dumps(
                {
                    "claim_token": str(self.run.item.claim_token),
                    "status": "failed",
                    "error_summary": "repository-private-secret is no longer selected",
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertNotIn("repository-private-secret", response.content.decode())
        self.run.refresh_from_db()
        self.assertEqual(self.run.state, Run.State.COMPLETED)

    @override_settings(VIBEARR_CREDENTIAL_KEY="")
    def test_admin_reports_snapshot_protection_before_deletion(self):
        from django.contrib.auth import get_user_model

        self.claim()
        self.deselect_repository()
        self.client.force_login(
            get_user_model().objects.create_superuser("reviewer", password="test")
        )
        response = self.client.post(
            reverse("admin:credentials_credential_delete", args=[self.repository.pk]),
            {"post": "yes"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Cannot delete")
        self.assertContains(response, str(self.run.pk))
        self.assertNotContains(response, "repository-private-secret")
        self.assertTrue(Credential.objects.filter(pk=self.repository.pk).exists())

    def test_admin_reports_snapshot_protection_in_bulk_action(self):
        from django.contrib.auth import get_user_model

        self.claim()
        self.deselect_repository()
        self.client.force_login(
            get_user_model().objects.create_superuser("reviewer", password="test")
        )
        response = self.client.post(
            reverse("admin:credentials_credential_changelist"),
            {
                "action": "delete_selected",
                "_selected_action": [self.repository.pk],
                "post": "yes",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Cannot delete")
        self.assertContains(response, str(self.run.pk))
        self.assertTrue(Credential.objects.filter(pk=self.repository.pk).exists())

    def test_snapshot_defaults_do_not_protect_unselected_credential(self):
        ProjectRunnerSettings.objects.create(
            project=self.project,
            overrides={
                "repository_credential_id": {
                    "mode": "replace",
                    "value": self.unselected.pk,
                },
            },
        )
        self.claim()
        self.deselect_repository()
        reference = self.repository.pk
        self.repository.delete()
        self.assertFalse(Credential.objects.filter(pk=reference).exists())

    def test_arbitrary_id_in_run_request_is_rejected(self):
        self.claim()
        response = self.post(
            {
                "scope": "run",
                "run_id": str(self.run.pk),
                "claim_token": str(self.run.item.claim_token),
                "purpose": "research",
                "credential_id": self.unselected.pk,
            }
        )
        self.assertEqual(response.status_code, 400)

    def test_removed_preflight_grant_blocks_retrieval(self):
        RunnerPreflightGrant.objects.filter(runner=self.runner).delete()
        self.assertEqual(self.preflight().status_code, 400)

    def test_revocation_blocks_active_run_retrieval(self):
        self.claim()
        self.repository.revoke()
        self.assertEqual(self.run_secret().status_code, 400)

    def test_wrong_kind_is_rejected_and_configuration_is_invalid(self):
        Credential.objects.filter(pk=self.repository.pk).update(kind="login")
        response = self.preflight()
        self.assertEqual(response.status_code, 400)
        self.assertIn("type", response.content.decode())
        codes = [
            entry["code"] for entry in configuration_document()["projects"][0]["errors"]
        ]
        self.assertIn("wrong_provider", codes)

    def test_malformed_body_fails_closed_without_caching(self):
        response = self.client.post(self.url, "{", content_type="application/json")
        self.assertEqual(response.status_code, 400)
        self.assertIn("no-store", response["Cache-Control"])

    def test_backend_change_blocks_active_run_delivery(self):
        self.claim()
        self.settings.implementation_backend = "claude"
        self.settings.implementation_model = "claude-sonnet-5-5"
        self.settings.save()
        self.assertEqual(self.run_secret("implementation").status_code, 400)

    def test_source_refresh_returns_owned_manual_snapshot(self):
        self.claim()
        response = self.client.post(
            reverse("runner_refresh_source", args=[self.run.pk]),
            json.dumps({"claim_token": str(self.run.item.claim_token)}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["source_snapshot"]["title"], "Task")

    def test_source_refresh_rejects_stale_token(self):
        self.claim()
        response = self.client.post(
            reverse("runner_refresh_source", args=[self.run.pk]),
            json.dumps({"claim_token": str(uuid.uuid4())}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 409)

    def test_source_refresh_fails_closed_on_provider_error(self):
        from unittest.mock import patch
        from work_queue.github import GitHubAPIError

        self.claim()
        with patch(
            "runner_control.source_api.refresh_issue_for_launch",
            side_effect=GitHubAPIError("offline"),
        ):
            response = self.client.post(
                reverse("runner_refresh_source", args=[self.run.pk]),
                json.dumps({"claim_token": str(self.run.item.claim_token)}),
                content_type="application/json",
            )
        self.assertEqual(response.status_code, 503)
        self.assertIn("wait before launch", response.json()["error"])

    def assert_source_refresh_revocation_is_read_only(self, identity_changes):
        from datetime import timedelta
        from unittest.mock import patch
        from django.utils import timezone
        from work_queue.models import QueueItem
        from work_queue.services import import_issue
        from work_queue.test_selection import source_issue, source_repository

        QueueItem.objects.filter(pk=self.item.pk).update(status="succeeded")
        Project.objects.filter(pk=self.project.pk).update(
            autonomous=True,
            enabled_at=timezone.now() - timedelta(minutes=1),
            github_repository_id=99,
            github_owner_id=42,
            github_default_branch="main",
        )
        self.project.refresh_from_db()
        original = source_issue()
        import_issue(project=self.project, **original)
        self.claim()
        before_item = QueueItem.objects.values().get(pk=self.run.item_id)
        before_run = Run.objects.values().get(pk=self.run.pk)
        before_project = Project.objects.values().get(pk=self.project.pk)

        def provider(project, number, token, **kwargs):
            RunnerIdentity.objects.filter(pk=self.runner.pk).update(**identity_changes)
            return {
                **original,
                "state": "closed",
                "revision": "c" * 64,
                "body": "Revoked request",
            }

        repository = source_repository(repository_url=self.project.repository_url)
        with (
            patch("work_queue.github.repository_metadata", return_value=repository),
            patch(
                "work_queue.github.issue_comments",
                return_value=[{"body": "Revoked comments"}],
            ),
            patch("work_queue.github.read_issue", side_effect=provider),
        ):
            response = self.client.post(
                reverse("runner_refresh_source", args=[self.run.pk]),
                json.dumps({"claim_token": str(self.run.item.claim_token)}),
                content_type="application/json",
            )
        self.assertEqual(response.status_code, 409, response.content)
        self.assertEqual(
            QueueItem.objects.values().get(pk=self.run.item_id), before_item
        )
        self.assertEqual(Run.objects.values().get(pk=self.run.pk), before_run)
        self.assertEqual(
            Project.objects.values().get(pk=self.project.pk), before_project
        )

    def test_source_refresh_rechecks_disabled_identity_before_side_effects(self):
        self.assert_source_refresh_revocation_is_read_only({"enabled": False})

    def test_source_refresh_rechecks_controller_proof_before_side_effects(self):
        self.assert_source_refresh_revocation_is_read_only(
            {"controller_digest": token_digest("rotated-controller")}
        )
