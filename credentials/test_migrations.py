import json

from cryptography.fernet import Fernet
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase, override_settings

from projects.models import Project

from .models import APISettings, Credential
from .test_models import TEST_KEY


@override_settings(NQ_CREDENTIAL_KEY="")
class CredentialMigrationTests(TransactionTestCase):
    old_target = [
        ("credentials", "0001_initial"),
        ("projects", "0003_project_credential"),
    ]
    new_target = [
        ("credentials", "0002_provider_and_types"),
        ("projects", "0003_project_credential"),
    ]

    def setUp(self):
        executor = MigrationExecutor(connection)
        self.addCleanup(self.restore_schema)
        executor.migrate(self.old_target)
        apps = executor.loader.project_state(self.old_target).apps
        old_credential = apps.get_model("credentials", "Credential")
        payload = json.dumps({"secret": "legacy-token", "passphrase": ""}).encode(
            "utf-8"
        )
        self.ciphertext = Fernet(TEST_KEY).encrypt(payload).decode("ascii")
        self.token = old_credential.objects.create(
            name="Legacy token", kind="github_token", encrypted_payload=self.ciphertext
        )
        self.ssh = old_credential.objects.create(
            name="Legacy SSH",
            kind="ssh_key",
            encrypted_payload="unchanged-ssh-ciphertext",
        )
        project = apps.get_model("projects", "Project")
        self.project = project.objects.create(
            name="Legacy project",
            repository_url="https://github.com/example/repo",
            credential_id=self.token.pk,
        )
        MigrationExecutor(connection).migrate(self.new_target)

    def restore_schema(self):
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())

    def test_existing_token_becomes_github_token_without_reencryption(self):
        credential = Credential.all_objects.defer(
            "secret_version", "revoked_at", "deleted_at"
        ).get(pk=self.token.pk)
        self.assertEqual(credential.provider, "github")
        self.assertEqual(credential.kind, "token")
        self.assertEqual(credential.encrypted_payload, self.ciphertext)
        with override_settings(NQ_CREDENTIAL_KEY=TEST_KEY):
            self.assertEqual(credential.get_secret()["secret"], "legacy-token")

    def test_existing_ssh_key_becomes_github_ssh_without_reencryption(self):
        credential = Credential.all_objects.defer(
            "secret_version", "revoked_at", "deleted_at"
        ).get(pk=self.ssh.pk)
        self.assertEqual(credential.provider, "github")
        self.assertEqual(credential.kind, "ssh")
        self.assertEqual(credential.encrypted_payload, "unchanged-ssh-ciphertext")

    def test_project_association_is_preserved(self):
        project = Project.all_objects.only("id", "credential_id").get(
            pk=self.project.pk
        )
        self.assertEqual(project.credential_id, self.token.pk)

    def test_existing_credentials_can_be_rolled_back(self):
        executor = MigrationExecutor(connection)
        executor.migrate(self.old_target)
        apps = executor.loader.project_state(self.old_target).apps
        old_credential = apps.get_model("credentials", "Credential")
        token = old_credential.objects.get(pk=self.token.pk)
        self.assertEqual(token.kind, "github_token")
        self.assertEqual(token.encrypted_payload, self.ciphertext)


class SyncIntervalMigrationTests(TransactionTestCase):
    def restore_schema(self):
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())

    def test_existing_settings_keep_credential_and_default_to_hourly(self):
        self.addCleanup(self.restore_schema)
        target = [("credentials", "0003_apisettings")]
        executor = MigrationExecutor(connection)
        executor.migrate(target)
        apps = executor.loader.project_state(target).apps
        credential = apps.get_model("credentials", "Credential").objects.create(
            name="Existing token",
            provider="github",
            kind="token",
            encrypted_payload="unchanged",
        )
        apps.get_model("credentials", "APISettings").objects.create(
            github_credential=credential
        )
        self.restore_schema()
        saved = APISettings.objects.get()
        self.assertEqual(saved.github_credential_id, credential.pk)
        self.assertEqual(saved.auto_sync_interval_hours, 1)
        self.assertIsNone(saved.last_auto_sync_at)


class BackendCredentialMigrationTests(TransactionTestCase):
    def restore_schema(self):
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())

    def test_repository_secrets_survive_backend_migration(self):
        self.addCleanup(self.restore_schema)
        target = [("credentials", "0004_auto_sync_interval")]
        executor = MigrationExecutor(connection)
        executor.migrate(target)
        apps = executor.loader.project_state(target).apps
        saved = apps.get_model("credentials", "Credential").objects.create(
            name="Existing repository",
            provider="github",
            kind="token",
            encrypted_payload="preserved-ciphertext",
        )
        self.restore_schema()
        saved = Credential.objects.get(pk=saved.pk)
        self.assertEqual(saved.encrypted_payload, "preserved-ciphertext")
        self.assertEqual(saved.secret_version, 1)
        self.assertIsNone(saved.revoked_at)


class PollingSecondsMigrationTests(TransactionTestCase):
    def restore_schema(self):
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())

    def test_explicit_long_interval_is_preserved_in_seconds(self):
        self.addCleanup(self.restore_schema)
        target = [("credentials", "0007_credentialsecretversion_and_more")]
        executor = MigrationExecutor(connection)
        executor.migrate(target)
        apps = executor.loader.project_state(target).apps
        apps.get_model("credentials", "APISettings").objects.create(
            auto_sync_interval_hours=12
        )
        self.restore_schema()
        self.assertEqual(APISettings.objects.get().auto_sync_interval_seconds, 43200)
