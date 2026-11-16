import os

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.core.validators import validate_slug
from django.core.exceptions import ValidationError
from django.db import transaction

from runner_control.auth import token_digest
from runner_control.models import RunnerController, RunnerIdentity


class Command(BaseCommand):
    help = "Provision, rotate, or disable an infrastructure runner identity."

    def add_arguments(self, parser):
        parser.add_argument("name")
        parser.add_argument("--token-env", default="NQ_RUNNER_API_TOKEN")
        parser.add_argument("--disable", action="store_true")

    def handle(self, *args, **options):
        name = options["name"]
        try:
            validate_slug(name)
        except ValidationError:
            raise CommandError("Use a slug for the runner name.") from None
        if not name or len(name) > 100:
            raise CommandError("Use a runner name of 1 to 100 characters.")
        with transaction.atomic():
            RunnerController.objects.select_for_update().get_or_create(pk=1)
            if options["disable"]:
                RunnerIdentity.objects.filter(name=name).update(enabled=False)
                self.stdout.write("Runner identity disabled.")
                return
            token = os.environ.get(options["token_env"], "")
            if len(token) < 32 or any(character.isspace() for character in token):
                raise CommandError(
                    "Set the token environment variable to at least 32 characters without whitespace."
                )
            if token == settings.NQ_API_TOKEN:
                raise CommandError("Use a separate token from NQ_API_TOKEN.")
            digest = token_digest(token)
            if (
                RunnerIdentity.objects.filter(token_digest=digest)
                .exclude(name=name)
                .exists()
            ):
                raise CommandError("This token is already assigned to another runner.")
            RunnerIdentity.objects.update_or_create(
                name=name, defaults={"token_digest": digest, "enabled": True}
            )
        self.stdout.write("Runner identity provisioned; token value was not stored.")
