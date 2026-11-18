import json

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.views.decorators.debug import sensitive_variables

from credentials.models import Credential
from vibearr_executor.account_client import AccountClient


class Command(BaseCommand):
    help = "Register one selected saved Codex login in protected Docker storage without refreshing or enabling execution."

    def add_arguments(self, parser):
        parser.add_argument("--credential-id", type=int, required=True)
        parser.add_argument("--credential-version", type=int, required=True)

    @sensitive_variables()
    def handle(self, *args, **options):
        try:
            client = AccountClient.from_environment()
            if client is None:
                raise ValueError("Configure the private Docker account service first.")
            with transaction.atomic():
                credential = (
                    Credential.objects.select_for_update()
                    .filter(
                        pk=options["credential_id"],
                        secret_version=options["credential_version"],
                        revoked_at__isnull=True,
                        provider="codex",
                        kind="session",
                    )
                    .first()
                )
                if credential is None:
                    raise ValueError(
                        "Select an existing unrevoked Codex session connection and its current version."
                    )
                reference = {"id": credential.pk, "version": credential.secret_version}
                result = client.register(reference, credential.get_secret()["secret"])
                if result != {"registered": True, "credential": reference}:
                    raise ValueError(
                        "Account service returned another connection's registration."
                    )
        except (ValueError, OSError, ValidationError):
            raise CommandError(
                "Codex account registration failed; verify the selected connection, version, private service and original encryption key."
            ) from None
        self.stdout.write(
            json.dumps(
                {
                    "registered": True,
                    "credential": reference,
                    "provider_refresh_performed": False,
                }
            )
        )
