"""Disposable fixtures for the Docker UI browser check; never use production data."""

import os


def main():
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "vibearr_app.settings")
    import django

    django.setup()
    from django.core.management import call_command
    from django.db import connection
    from django.utils import timezone
    from projects.models import Project
    from work_queue.models import QueueItem
    from runner_control.models import Run, RunnerIdentity

    database = connection.settings_dict
    if database["NAME"] != "vibearr_ui" or database["HOST"] != "vibearr-ui-check-db":
        raise RuntimeError(
            "UI fixtures require the isolated vibearr-ui-check-db database."
        )
    call_command("migrate", verbosity=0, interactive=False)
    project = Project.objects.create(
        name="vibearr UI preview", repository_url="https://github.com/example/ui"
    )
    QueueItem.objects.create(
        project=project, title="Add keyboard shortcuts to the queue", priority="high"
    )
    active = QueueItem.objects.create(
        project=project,
        title="Improve queue navigation",
        status="running",
        priority="high",
    )
    completed = QueueItem.objects.create(
        project=project,
        title="Preserve queue filters between views",
        status="succeeded",
    )
    QueueItem.objects.create(
        project=project,
        title="Handle malformed queue filters",
        status="failed",
        error_summary="Validation failed: expected four visible statuses.",
    )
    runner = RunnerIdentity.objects.create(
        name="docker-worker-preview", token_digest="x" * 64
    )
    Run.objects.create(
        runner=runner,
        item=active,
        state="active",
        active_slot=1,
        stage="waiting",
        configuration_snapshot={"execution_kind": "native"},
        wait_reason="Waiting for required CI checks",
        heartbeat_at=timezone.now(),
        native_status={
            "validation_status": "passed",
            "repair_count": 1,
            "detail": "Candidate tests passed. Waiting for remote CI before delivery completes.",
        },
    )
    Run.objects.create(
        runner=runner,
        item=completed,
        state="completed",
        stage="finished",
        result_status="succeeded",
        native_outcome="succeeded",
        result_url="https://github.com/example/ui/pull/7",
        finished_at=timezone.now(),
        configuration_snapshot={"execution_kind": "native"},
        native_status={"validation_status": "passed", "repair_count": 0},
    )
    call_command("runserver", "0.0.0.0:8000", use_reloader=False)


if __name__ == "__main__":
    main()
