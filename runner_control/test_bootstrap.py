from copy import deepcopy
from datetime import datetime, timezone
from io import StringIO
import json
from pathlib import Path
import tempfile
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext

from credentials.models import APISettings, Credential, RunnerPreflightGrant
from credentials.test_models import TEST_KEY
from projects.models import Project
from vibearr_runner.policy import default_execution_policy
from work_queue.models import MergeEvidence

from .auth import token_digest
from .bootstrap import OWNER_ID, REPOSITORY_ID, REPOSITORY_URL, bootstrap, inspect
from .bootstrap_diagnostics import diagnose
from .management.commands.seed_self_development import (
    record_prerequisites,
    verified_prerequisites,
)
from .models import RunnerController, RunnerIdentity
from .settings_models import ProjectRunnerSettings, RunnerSettings


REPOSITORY = {
    "repository_id": REPOSITORY_ID,
    "owner_id": OWNER_ID,
    "repository_url": REPOSITORY_URL,
    "default_branch": "main",
}


@override_settings(NQ_CREDENTIAL_KEY=TEST_KEY, NQ_API_TOKEN="generic-queue-token")
class BootstrapTests(TestCase):
    def setUp(self):
        self.credentials = {}
        for purpose in ("import", "issue", "repository", "backend"):
            credential = Credential(
                name=purpose,
                provider="codex" if purpose == "backend" else "github",
                kind="token",
            )
            credential.set_secret(f"{purpose}-private-value")
            credential.save()
            self.credentials[purpose] = credential
        policy = default_execution_policy()
        policy.update(
            runtime_profile="codex-token-v1",
            validation_profile="vibearr-python312-v1",
            network_hosts=["api.openai.com"],
        )
        self.request = {
            "schema_version": 1,
            "import_credential_id": self.credentials["import"].pk,
            "issue_credential_id": self.credentials["issue"].pk,
            "repository_credential_id": self.credentials["repository"].pk,
            "backend": "codex",
            "model": "selected-model",
            "backend_credential_id": self.credentials["backend"].pk,
            "runner_name": "self-development",
            "runner_token_env": "VIBEARR_TEST_BOOTSTRAP_TOKEN",
            "execution_policy": policy,
        }
        self.token = "dedicated-bootstrap-runner-token-32-characters"
        self.environment = patch.dict(
            "os.environ", {"VIBEARR_TEST_BOOTSTRAP_TOKEN": self.token}
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.provider = patch(
            "runner_control.bootstrap.repository_metadata",
            return_value=REPOSITORY.copy(),
        )
        self.provider.start()
        self.addCleanup(self.provider.stop)

    def test_preview_performs_no_database_writes_even_without_singletons(self):
        RunnerSettings.objects.all().delete()
        with CaptureQueriesContext(connection) as queries:
            result = bootstrap(self.request)
        self.assertTrue(result["ready_to_apply"])
        self.assertFalse(result["applied"])
        writes = [
            entry["sql"]
            for entry in queries
            if entry["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))
        ]
        self.assertEqual(writes, [])
        self.assertFalse(RunnerSettings.objects.exists())
        self.assertFalse(Project.objects.exists())
        self.assertFalse(APISettings.objects.exists())
        self.assertFalse(RunnerController.objects.exists())

    def test_apply_reuses_project_and_preserves_original_cutoff(self):
        project = Project.objects.create(
            name="My saved name",
            repository_url=REPOSITORY_URL,
            autonomous=True,
            base_branch="main",
            permitted_author_ids=[OWNER_ID],
        )
        cutoff = datetime(2026, 11, 16, 10, 27, 54, 822161, tzinfo=timezone.utc)
        Project.objects.filter(pk=project.pk).update(
            github_repository_id=REPOSITORY_ID,
            github_owner_id=OWNER_ID,
            github_default_branch="main",
            enabled_at=cutoff,
        )
        result = bootstrap(self.request, apply=True)
        project.refresh_from_db()
        self.assertTrue(result["applied"])
        self.assertEqual(result["project_id"], project.pk)
        self.assertEqual(project.name, "My saved name")
        self.assertEqual(project.enabled_at, cutoff)
        self.assertFalse(project.backfill_issues)
        self.assertEqual(Project.objects.count(), 1)

    def test_repeated_apply_preserves_secrets_identity_proof_and_authority_revision(
        self,
    ):
        first = bootstrap(self.request, apply=True)
        runner = RunnerIdentity.objects.get(name="self-development")
        runner.controller_digest = token_digest("original-controller-proof")
        runner.save(update_fields=["controller_digest"])
        project = Project.objects.get(pk=first["project_id"])
        original = (project.enabled_at, project.github_authority_version)
        secrets = list(
            Credential.objects.values_list("pk", "encrypted_payload", "secret_version")
        )
        with CaptureQueriesContext(connection) as queries:
            second = bootstrap(self.request, apply=True)
        self.assertTrue(second["applied"])
        project.refresh_from_db()
        runner.refresh_from_db()
        self.assertEqual(
            (project.enabled_at, project.github_authority_version), original
        )
        self.assertEqual(
            runner.controller_digest, token_digest("original-controller-proof")
        )
        self.assertEqual(runner.token_digest, token_digest(self.token))
        self.assertEqual(
            list(
                Credential.objects.values_list(
                    "pk", "encrypted_payload", "secret_version"
                )
            ),
            secrets,
        )
        self.assertFalse(
            any(
                entry["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))
                for entry in queries
            )
        )

    def test_apply_does_not_enable_global_execution(self):
        bootstrap(self.request, apply=True, mvp_defaults=True)
        self.assertFalse(RunnerSettings.objects.get(pk=1).enabled)
        self.assertEqual(RunnerSettings.objects.get(pk=1).schedule_mode, "continuous")
        self.assertFalse(RunnerSettings.objects.get(pk=1).reserve_enabled)
        self.assertEqual(RunnerSettings.objects.get(pk=1).polling_interval_seconds, 30)
        self.assertEqual(
            RunnerSettings.objects.get(pk=1).implementation_timeout_seconds, 1800
        )
        self.assertEqual(
            RunnerSettings.objects.get(pk=1).validation_timeout_seconds, 1200
        )

    def test_custom_global_time_budget_is_not_overwritten(self):
        saved = RunnerSettings.objects.get(pk=1)
        saved.implementation_timeout_seconds = 600
        saved.save()
        result = bootstrap(self.request, apply=True)
        self.assertTrue(result["applied"])
        saved.refresh_from_db()
        self.assertEqual(saved.implementation_timeout_seconds, 600)
        self.assertTrue(Project.objects.exists())

    def test_values_equal_to_framework_defaults_are_preserved_without_explicit_option(
        self,
    ):
        bootstrap(self.request, apply=True)
        saved = RunnerSettings.objects.get(pk=1)
        self.assertEqual(saved.polling_interval_seconds, 60)
        self.assertEqual(saved.implementation_timeout_seconds, 3600)
        self.assertEqual(saved.validation_timeout_seconds, 1800)

    def test_explicit_global_defaults_are_previewed_without_writes(self):
        with CaptureQueriesContext(connection) as queries:
            result = bootstrap(self.request, mvp_defaults=True)
        self.assertTrue(
            any(
                "Explicitly set global polling_interval_seconds from 60 to 30" in value
                for value in result["changes"]
            )
        )
        self.assertEqual(RunnerSettings.objects.get(pk=1).polling_interval_seconds, 60)
        self.assertFalse(
            any(
                entry["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))
                for entry in queries
            )
        )

    def test_existing_windows_pause_and_reserve_are_preserved(self):
        saved = RunnerSettings.objects.get(pk=1)
        saved.schedule_mode = "windows"
        saved.windows = [
            {"days": [0, 1, 2, 3, 4, 5, 6], "start": "00:00", "end": "23:59"}
        ]
        saved.paused = True
        saved.reserve_enabled = True
        saved.save()
        self.assertTrue(bootstrap(self.request, apply=True)["applied"])
        saved.refresh_from_db()
        self.assertEqual(saved.schedule_mode, "windows")
        self.assertTrue(saved.paused)
        self.assertTrue(saved.reserve_enabled)
        diagnostics = diagnose(self.request, REPOSITORY)
        reserve = next(
            value
            for value in diagnostics["components"]
            if value["component"] == "usage_reserve"
        )
        self.assertEqual(reserve["status"], "wait")

    def test_apply_grants_only_explicit_selected_runner_credentials(self):
        unselected = Credential(name="other", provider="codex", kind="token")
        unselected.set_secret("other-private-value")
        unselected.save()
        bootstrap(self.request, apply=True)
        expected = {
            self.credentials[purpose].pk
            for purpose in ("issue", "repository", "backend")
        }
        self.assertEqual(
            set(RunnerPreflightGrant.objects.values_list("credential_id", flat=True)),
            expected,
        )

    def test_import_credential_does_not_implicitly_select_delivery(self):
        request = deepcopy(self.request)
        request["repository_credential_id"] = request["import_credential_id"]
        with self.assertRaisesMessage(ValidationError, "separate import"):
            bootstrap(request, apply=True)
        self.assertFalse(Project.objects.exists())

    def test_conflicting_existing_project_choice_is_not_overwritten(self):
        project = Project.objects.create(
            name="Existing",
            repository_url=REPOSITORY_URL,
            autonomous=True,
            base_branch="release",
            permitted_author_ids=[OWNER_ID],
        )
        result = bootstrap(self.request, apply=True)
        self.assertFalse(result["applied"])
        self.assertTrue(any("base_branch" in value for value in result["conflicts"]))
        project.refresh_from_db()
        self.assertEqual(project.base_branch, "release")

    def test_existing_backend_override_is_preserved(self):
        bootstrap(self.request, apply=True)
        request = deepcopy(self.request)
        request["model"] = "different-model"
        result = bootstrap(request, apply=True)
        self.assertFalse(result["applied"])
        overrides = ProjectRunnerSettings.objects.get().overrides
        self.assertEqual(overrides["implementation_model"]["value"], "selected-model")

    def test_explicit_inheritance_is_not_replaced_by_a_frozen_override(self):
        bootstrap(self.request, apply=True)
        saved = RunnerSettings.objects.get(pk=1)
        saved.implementation_model = "selected-model"
        saved.save()
        project = ProjectRunnerSettings.objects.get()
        project.overrides["implementation_model"] = {"mode": "inherit"}
        project.save()
        self.assertTrue(bootstrap(self.request, apply=True)["applied"])
        project.refresh_from_db()
        self.assertEqual(project.overrides["implementation_model"], {"mode": "inherit"})

    def test_stale_provider_observation_does_not_roll_back_accepted_authority(self):
        initial = bootstrap(self.request, apply=True)

        def delayed(*args):
            Project.objects.filter(pk=initial["project_id"]).update(
                repository_url="https://github.com/innateessence/renamed.git",
                github_authority_version=99,
            )
            return REPOSITORY.copy()

        with patch("runner_control.bootstrap.repository_metadata", side_effect=delayed):
            with self.assertRaisesMessage(ValidationError, "authority changed"):
                bootstrap(self.request, apply=True)
        project = Project.objects.get(pk=initial["project_id"])
        self.assertEqual(project.github_authority_version, 99)
        self.assertEqual(
            project.repository_url, "https://github.com/innateessence/renamed.git"
        )

    def test_legacy_project_credential_is_preserved_separately_from_auto_import(self):
        project = Project.objects.create(
            name="Existing",
            repository_url=REPOSITORY_URL,
            autonomous=True,
            base_branch="main",
            permitted_author_ids=[OWNER_ID],
            credential=self.credentials["repository"],
        )
        result = bootstrap(self.request, apply=True)
        self.assertTrue(result["applied"])
        project.refresh_from_db()
        self.assertEqual(project.credential_id, self.credentials["repository"].pk)
        self.assertEqual(
            APISettings.objects.get(pk=1).github_credential_id,
            self.credentials["import"].pk,
        )

    def test_existing_runner_token_is_never_rotated(self):
        bootstrap(self.request, apply=True)
        with patch.dict(
            "os.environ",
            {
                "VIBEARR_TEST_BOOTSTRAP_TOKEN": "different-dedicated-runner-token-32-characters"
            },
        ):
            result = bootstrap(self.request, apply=True)
        self.assertFalse(result["applied"])
        self.assertEqual(
            RunnerIdentity.objects.get().token_digest, token_digest(self.token)
        )

    def test_duplicate_repository_projects_are_rejected(self):
        for name in ("First", "Second"):
            Project.objects.create(name=name, repository_url=REPOSITORY_URL)
        with self.assertRaisesMessage(ValidationError, "Multiple projects"):
            bootstrap(self.request)

    def test_provider_owner_mismatch_is_rejected(self):
        repository = {**REPOSITORY, "owner_id": 999}
        with self.assertRaisesMessage(ValidationError, "Provider repository/owner"):
            inspect(self.request, repository)

    def test_missing_credentials_are_consolidated_without_database_writes(self):
        request = deepcopy(self.request)
        for offset, purpose in enumerate(("import", "issue", "repository", "backend")):
            request[f"{purpose}_credential_id"] = 1000 + offset
        with CaptureQueriesContext(connection) as queries:
            result = diagnose(request, REPOSITORY)
        errors = {
            value["component"]
            for value in result["components"]
            if value["status"] == "error"
        }
        self.assertTrue(
            {
                "credential.import",
                "credential.issue",
                "credential.repository",
                "credential.backend",
            }
            <= errors
        )
        self.assertFalse(result["ready"])
        self.assertEqual(result["model_calls"], 0)
        self.assertFalse(
            any(
                entry["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))
                for entry in queries
            )
        )

    def test_default_seed_export_does_not_use_provider_or_database(self):
        output = StringIO()
        with (
            CaptureQueriesContext(connection) as queries,
            patch(
                "runner_control.management.commands.seed_self_development.NativeGitHub"
            ) as github,
        ):
            call_command("seed_self_development", stdout=output)
        self.assertEqual(len(json.loads(output.getvalue())["features"]), 16)
        github.assert_not_called()
        self.assertEqual(len(queries), 0)

    def test_missing_encryption_key_is_actionable_without_replacing_credentials(self):
        from vibearr_runner.bootstrap_profiles import save_new

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "bootstrap.json"
            save_new(path, self.request)
            with override_settings(NQ_CREDENTIAL_KEY=""):
                with self.assertRaisesMessage(
                    CommandError, "Configure VIBEARR_CREDENTIAL_KEY"
                ):
                    call_command(
                        "bootstrap_self_development",
                        configuration=path,
                        stdout=StringIO(),
                    )
        self.assertEqual(Credential.objects.count(), 4)
        self.assertFalse(Project.objects.exists())


class PrerequisiteTests(TestCase):
    def setUp(self):
        self.mapping = {"feature": "05", "issue": 69, "pull": 55, "merge_sha": "a" * 40}
        self.merged = "2026-11-16T10:27:27+00:00"
        self.pull = {
            "merged": True,
            "merge_commit_sha": "a" * 40,
            "merged_at": self.merged,
            "base": {"ref": "main", "repo": {"id": REPOSITORY_ID}},
            "head": {"repo": {"id": REPOSITORY_ID}},
        }
        self.issue = {
            "number": 69,
            "user": {"id": OWNER_ID},
            "body": "<!-- vibearr-plan-feature: 05 -->",
            "state": "closed",
        }

    def github(self):
        from unittest.mock import Mock

        return Mock(
            request=Mock(return_value=self.issue), pull=Mock(return_value=self.pull)
        )

    def test_closed_issue_does_not_substitute_for_provider_merge(self):
        self.pull["merged"] = False
        with self.assertRaisesMessage(ValidationError, "provider-verified merge"):
            verified_prerequisites(self.github(), [self.mapping])
        self.assertFalse(MergeEvidence.objects.exists())

    def test_parent_branch_merge_cannot_seed_main_prerequisite(self):
        self.pull["base"]["ref"] = "feat/unattended-issue-to-pr"
        with self.assertRaises(ValidationError):
            verified_prerequisites(self.github(), [self.mapping])

    def test_foreign_repository_merge_is_rejected(self):
        self.pull["head"]["repo"]["id"] = 42
        with self.assertRaises(ValidationError):
            verified_prerequisites(self.github(), [self.mapping])

    def test_expected_sha_must_match_actual_provider_merge(self):
        self.pull["merge_commit_sha"] = "b" * 40
        with self.assertRaises(ValidationError):
            verified_prerequisites(self.github(), [self.mapping])

    def test_repeated_verified_seed_preserves_existing_provenance(self):
        verified = verified_prerequisites(self.github(), [self.mapping])
        first = record_prerequisites(verified)
        MergeEvidence.objects.filter(pk=first[0]).update(
            provenance="earlier-trusted-bootstrap", operation_id="earlier-operation"
        )
        second = record_prerequisites(verified)
        self.assertEqual(first, second)
        self.assertEqual(MergeEvidence.objects.count(), 1)
        self.assertEqual(
            MergeEvidence.objects.get().provenance, "earlier-trusted-bootstrap"
        )

    def test_conflicting_existing_merge_evidence_cannot_be_replaced(self):
        verified = verified_prerequisites(self.github(), [self.mapping])
        record_prerequisites(verified)
        MergeEvidence.objects.update(merge_sha="b" * 40)
        with self.assertRaisesMessage(ValidationError, "immutable prerequisite"):
            record_prerequisites(verified)
