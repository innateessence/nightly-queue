"""Explicit disposable data for the Docker backup/restore acceptance check."""

import json
import os
import sys
from uuid import uuid4


NAME = "Docker backup acceptance fixture"
SECRET = "synthetic-backup-proof-without-provider-access"


def main():
    if os.environ.get("VIBEARR_DISPOSABLE_ACCEPTANCE") != "1" or sys.argv[1:] not in (
        ["seed"],
        ["verify"],
    ):
        raise RuntimeError(
            "Explicitly select seed/verify in a disposable acceptance project."
        )
    import django

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "vibearr_app.settings")
    django.setup()
    from django.db import transaction
    from django.utils import timezone
    from credentials.models import Credential
    from projects.models import Project
    from runner_control.models import Run, RunEvidence, RunnerIdentity
    from work_queue.models import QueueItem

    if sys.argv[1] == "seed":
        with transaction.atomic():
            if (
                Credential.objects.filter(name=NAME).exists()
                or Project.objects.filter(name=NAME).exists()
            ):
                raise RuntimeError(
                    "Acceptance fixture already exists; preserve its history."
                )
            credential = Credential(name=NAME, provider="github", kind="token")
            credential.set_secret(SECRET)
            credential.save()
            project = Project.objects.create(
                name=NAME,
                repository_url="https://github.com/example/backup-fixture",
                autonomous=False,
            )
            queued = QueueItem.objects.create(
                project=project,
                title=NAME + " queued",
                instructions="Preserve the queued work.",
            )
            finished = QueueItem.objects.create(
                project=project,
                title=NAME + " finished",
                status="succeeded",
                finished_at=timezone.now(),
            )
            identity = RunnerIdentity.objects.create(
                name="backup-fixture-runner", enabled=False, token_digest="f" * 64
            )
            run = Run.objects.create(
                runner=identity,
                item=finished,
                state="completed",
                stage="finished",
                finished_at=timezone.now(),
                result_status="succeeded",
            )
            RunEvidence.objects.create(
                run=run,
                operation_id=uuid4(),
                kind="backup_fixture",
                payload={"synthetic": True, "queued_item": queued.pk},
            )
    credential = Credential.objects.get(name=NAME)
    if credential.get_secret()["secret"] != SECRET:
        raise RuntimeError(
            "Restored encryption key cannot decrypt the saved credential."
        )
    project = Project.objects.get(name=NAME)
    if (
        project.autonomous
        or project.items.filter(status="queued").count() != 1
        or project.items.filter(status="succeeded").count() != 1
    ):
        raise RuntimeError(
            "Queue state or project authority changed during restoration."
        )
    run = Run.objects.get(item__project=project)
    if (
        run.state != "completed"
        or run.evidence.get(kind="backup_fixture").payload.get("synthetic") is not True
    ):
        raise RuntimeError("Run history or evidence changed during restoration.")
    print(
        json.dumps(
            {
                "kind": "disposable-backup-fixture",
                "action": sys.argv[1],
                "credential_decrypted": True,
                "queue_and_history_preserved": True,
            }
        )
    )


if __name__ == "__main__":
    main()
