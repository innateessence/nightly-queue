import fcntl
import os
from pathlib import Path
import stat
from uuid import UUID

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.core.validators import validate_slug
from django.db import transaction

from credentials.models import Credential, RunnerPreflightGrant
from deploy.docker.initialize import write_once
from runner_control.auth import token_digest
from runner_control.models import RunnerController, RunnerIdentity
from runner_control.settings_models import RunnerSettings
from vibearr_runner.bootstrap_files import private_json


ROOT = Path("/run/vibearr/runner")
STATE = Path("/var/lib/vibearr")


class Command(BaseCommand):
    help = "Create a Docker runner token/identity and explicitly selected credential grants without enabling execution."

    def add_arguments(self, parser):
        parser.add_argument("name")
        parser.add_argument("--credential-id", type=int, action="append", default=[])
        parser.add_argument(
            "--restored-backup",
            help="Explicitly archive restored authority and create a new runner identity.",
        )

    def handle(self, *args, **options):
        name = options["name"]
        try:
            validate_slug(name)
        except ValidationError:
            raise CommandError("Use a runner slug of 1 to 100 characters.") from None
        if not 1 <= len(name) <= 100:
            raise CommandError("Use a runner slug of 1 to 100 characters.")
        directory = ROOT.lstat()
        if (
            not stat.S_ISDIR(directory.st_mode)
            or directory.st_uid != os.geteuid()
            or directory.st_mode & 0o077
        ):
            raise CommandError("Initialize the private Docker runner volume first.")
        descriptor = os.open(
            ROOT / "bootstrap.lock", os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600
        )
        with os.fdopen(descriptor, "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            marker = ROOT / "restore-required.json"
            if marker.exists():
                self.retire_restored(name, options["restored_backup"])
            elif options["restored_backup"]:
                raise CommandError("This runner volume has no pending restore marker.")
            self.provision(name, set(options["credential_id"]))
            marker.unlink(missing_ok=True)
        self.stdout.write(
            "Docker runner identity and selected grants saved; execution remains unchanged."
        )

    def retire_restored(self, name, backup_id):
        marker = private_json(ROOT / "restore-required.json", 65536)
        try:
            valid = str(UUID(backup_id)) == backup_id == marker["backup_id"]
        except (ValueError, TypeError, KeyError, AttributeError):
            valid = False
        if not valid:
            raise CommandError("Select the exact restored backup UUID explicitly.")
        if (
            RunnerIdentity.objects.filter(enabled=True).exists()
            or RunnerSettings.objects.filter(enabled=True).exists()
        ):
            raise CommandError(
                "Fence restored identities and disable processing before new bootstrap."
            )
        if RunnerIdentity.objects.filter(name=name).exists():
            raise CommandError("Choose a new name for the restored Docker runner.")
        history = STATE / "restore-history" / backup_id
        history.mkdir(mode=0o700, parents=True, exist_ok=True)
        for path in STATE.iterdir():
            if path.name != "restore-history":
                if (history / path.name).exists():
                    raise CommandError(
                        "Restored history already contains this path; reconcile it explicitly."
                    )
                path.rename(history / path.name)
        for name in (
            "api-token",
            "validation-profile.json",
            "docker-qualification.json",
        ):
            path = ROOT / name
            retired = ROOT / ("retired-" + name + "." + backup_id)
            if path.exists() and not retired.exists():
                path.rename(retired)

    def provision(self, name, selected):
        with transaction.atomic():
            RunnerController.objects.select_for_update().get_or_create(pk=1)
            credentials = Credential.objects.select_for_update().filter(
                pk__in=selected, revoked_at__isnull=True
            )
            if set(credentials.values_list("pk", flat=True)) != selected:
                raise CommandError(
                    "Select existing, unrevoked credential IDs explicitly."
                )
            identity = RunnerIdentity.objects.filter(name=name).first()
            path = ROOT / "api-token"
            if identity and (not path.exists() or not identity.enabled):
                raise CommandError(
                    "Existing identity is disabled or its saved token is missing; reconcile explicitly."
                )
            write_once(path, "", uid=os.geteuid(), gid=os.getegid())
            token = path.read_text().strip()
            if (
                len(token) < 32
                or any(value.isspace() for value in token)
                or token == settings.VIBEARR_API_TOKEN
            ):
                raise CommandError(
                    "The saved runner token must be dedicated and private."
                )
            digest = token_digest(token)
            if identity and identity.token_digest != digest:
                raise CommandError(
                    "Saved token differs from this identity; bootstrap never rotates credentials."
                )
            if (
                RunnerIdentity.objects.filter(token_digest=digest)
                .exclude(name=name)
                .exists()
            ):
                raise CommandError(
                    "The saved token belongs to a different runner identity."
                )
            if identity is None:
                identity = RunnerIdentity.objects.create(name=name, token_digest=digest)
            for credential in credentials:
                RunnerPreflightGrant.objects.get_or_create(
                    runner=identity, credential=credential
                )
