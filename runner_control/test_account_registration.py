from io import StringIO
import json
from pathlib import Path
import tempfile
from unittest.mock import patch

from cryptography.fernet import Fernet
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django.utils import timezone

from credentials.models import Credential
from vibearr_executor.account_leases import AccountLeases
from vibearr_executor.contracts import canonical
from vibearr_executor.runtime import durable
from vibearr_executor.test_codex_account import account_secret


@override_settings(NQ_CREDENTIAL_KEY=Fernet.generate_key().decode())
class AccountRegistrationTests(TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.accounts = AccountLeases(Path(temporary.name), self.no_refresh)
        self.credential = Credential(
            name="Selected Codex account", provider="codex", kind="session"
        )
        self.credential.set_secret(account_secret(expired=True))
        self.credential.save()
        connection = patch(
            "runner_control.management.commands.docker_register_codex_account.AccountClient.from_environment",
            return_value=self.accounts,
        )
        connection.start()
        self.addCleanup(connection.stop)

    def no_refresh(self, home, *, force):
        self.fail("Registration must not contact the provider.")

    def register(self, *, version=None):
        output = StringIO()
        call_command(
            "docker_register_codex_account",
            credential_id=self.credential.pk,
            credential_version=version or self.credential.secret_version,
            stdout=output,
        )
        return json.loads(output.getvalue())

    def test_selected_expired_login_can_be_registered_without_refresh(self):
        result = self.register()
        self.assertTrue(result["registered"])
        self.assertFalse(result["provider_refresh_performed"])
        self.assertNotIn("private-refresh-token", json.dumps(result))
        self.assertTrue(
            (
                self.accounts.store.root / f"{self.credential.pk}-1/codex/auth.json"
            ).exists()
        )

    def test_stale_connection_version_is_rejected_before_private_storage(self):
        with self.assertRaises(CommandError):
            self.register(version=2)
        self.assertEqual(list(self.accounts.store.root.iterdir()), [])

    def test_revoked_connection_cannot_be_registered(self):
        Credential.objects.filter(pk=self.credential.pk).update(
            revoked_at=timezone.now()
        )
        with self.assertRaises(CommandError):
            self.register()
        self.assertEqual(list(self.accounts.store.root.iterdir()), [])

    def test_non_session_connections_cannot_be_registered(self):
        Credential.objects.filter(pk=self.credential.pk).update(kind="token")
        with self.assertRaises(CommandError):
            self.register()
        self.assertEqual(list(self.accounts.store.root.iterdir()), [])

    def test_registration_never_replaces_the_rotated_cache_with_the_original_seed(self):
        self.register()
        path = self.accounts.store.root / f"{self.credential.pk}-1/codex/auth.json"
        account = json.loads(path.read_bytes())
        account["tokens"]["refresh_token"] = "rotated-private-provider-refresh-0000000"
        durable(path, canonical(account))
        self.register()
        self.assertEqual(
            json.loads(path.read_bytes())["tokens"]["refresh_token"],
            "rotated-private-provider-refresh-0000000",
        )
