from io import StringIO
import json
import os
from pathlib import Path
import tempfile
from unittest.mock import patch
from uuid import uuid4

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings

from credentials.models import Credential, RunnerPreflightGrant
from credentials.test_models import TEST_KEY
from runner_control.auth import token_digest
from runner_control.models import RunnerIdentity


@override_settings(VIBEARR_CREDENTIAL_KEY=TEST_KEY)
class DockerBootstrapTests(TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.root.chmod(0o700)
        selected = patch(
            "runner_control.management.commands.docker_bootstrap_runner.ROOT", self.root
        )
        selected.start()
        self.addCleanup(selected.stop)
        self.output = StringIO()
        self.state = self.root / "state-volume"
        self.state.mkdir(mode=0o700)
        selected_state = patch(
            "runner_control.management.commands.docker_bootstrap_runner.STATE",
            self.state,
        )
        selected_state.start()
        self.addCleanup(selected_state.stop)

    def bootstrap(self):
        call_command("docker_bootstrap_runner", "docker-runner", stdout=self.output)

    def test_retry_preserves_the_persisted_token_and_identity(self):
        self.bootstrap()
        token = (self.root / "api-token").read_bytes()
        identity = RunnerIdentity.objects.get()
        self.bootstrap()
        self.assertEqual((self.root / "api-token").read_bytes(), token)
        self.assertEqual(RunnerIdentity.objects.get().pk, identity.pk)

    def test_generated_token_is_never_printed(self):
        self.bootstrap()
        token = (self.root / "api-token").read_text().strip()
        self.assertNotIn(token, self.output.getvalue())
        self.assertEqual(RunnerIdentity.objects.get().token_digest, token_digest(token))

    def test_existing_identity_without_its_token_requires_reconciliation(self):
        RunnerIdentity.objects.create(name="docker-runner", token_digest="a" * 64)
        with self.assertRaisesRegex(CommandError, "missing"):
            self.bootstrap()
        self.assertFalse((self.root / "api-token").exists())

    def test_saved_token_cannot_rotate_an_existing_identity(self):
        RunnerIdentity.objects.create(name="docker-runner", token_digest="a" * 64)
        path = self.root / "api-token"
        path.write_text("different-private-token-01234567890123456789")
        path.chmod(0o600)
        with self.assertRaisesRegex(CommandError, "never rotates"):
            self.bootstrap()
        self.assertEqual(RunnerIdentity.objects.get().token_digest, "a" * 64)

    def test_unsafe_runner_directory_cannot_receive_a_token(self):
        self.root.chmod(0o755)
        with self.assertRaisesRegex(CommandError, "private Docker"):
            self.bootstrap()
        self.assertFalse((self.root / "api-token").exists())

    def test_token_is_published_with_private_permissions(self):
        self.bootstrap()
        path = self.root / "api-token"
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(path.stat().st_uid, os.geteuid())

    def restored_marker(self):
        backup_id = str(uuid4())
        marker = self.root / "restore-required.json"
        marker.write_text(json.dumps({"backup_id": backup_id}))
        marker.chmod(0o600)
        return backup_id

    def test_restored_state_requires_the_explicit_backup_identity(self):
        self.restored_marker()
        with self.assertRaisesRegex(CommandError, "exact restored"):
            self.bootstrap()
        self.assertFalse((self.root / "api-token").exists())

    def test_restore_bootstrap_does_not_reuse_the_previous_controller_proof(self):
        backup_id = self.restored_marker()
        token = "restored-private-token-01234567890123456789"
        path = self.root / "api-token"
        path.write_text(token)
        path.chmod(0o600)
        old = RunnerIdentity.objects.create(
            name="old-runner", enabled=False, token_digest=token_digest(token)
        )
        journal = self.state / "journal.json"
        journal.write_text('{"controller_secret":"old-proof"}')
        call_command(
            "docker_bootstrap_runner",
            "new-runner",
            restored_backup=backup_id,
            stdout=self.output,
        )
        self.assertNotEqual(path.read_text().strip(), token)
        self.assertFalse(journal.exists())
        self.assertEqual(
            (self.state / "restore-history" / backup_id / "journal.json").read_text(),
            '{"controller_secret":"old-proof"}',
        )
        self.assertFalse((self.root / "restore-required.json").exists())
        old.refresh_from_db()
        self.assertFalse(old.enabled)

    def test_grants_are_limited_to_the_explicit_selected_credential(self):
        selected = Credential(name="selected", provider="github", kind="token")
        selected.set_secret("synthetic-selected-token")
        selected.save()
        other = Credential(name="other", provider="github", kind="token")
        other.set_secret("synthetic-other-token")
        other.save()
        call_command(
            "docker_bootstrap_runner",
            "docker-runner",
            credential_id=[selected.pk],
            stdout=self.output,
        )
        self.assertEqual(
            list(RunnerPreflightGrant.objects.values_list("credential_id", flat=True)),
            [selected.pk],
        )

    def test_missing_selected_credentials_cannot_create_an_identity(self):
        with self.assertRaisesRegex(CommandError, "unrevoked credential IDs"):
            call_command(
                "docker_bootstrap_runner",
                "docker-runner",
                credential_id=[123456789],
                stdout=self.output,
            )
        self.assertFalse(RunnerIdentity.objects.exists())
        self.assertFalse((self.root / "api-token").exists())
