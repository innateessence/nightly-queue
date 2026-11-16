import uuid
from datetime import timedelta

from django.core.exceptions import ValidationError
from django.core.validators import URLValidator
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from credentials.models import APISettings
from projects.models import Project
from projects.validators import normalize_repository_url

from .models import QueueItem


def claim_auto_sync(saved):
    now = timezone.now()
    cutoff = now - timedelta(hours=saved.auto_sync_interval_hours)
    due = Q(last_auto_sync_at__isnull=True) | Q(last_auto_sync_at__lte=cutoff)
    matching = APISettings.objects.filter(pk=saved.pk).filter(due)
    return matching.update(last_auto_sync_at=now) == 1


def enqueue_item(
    *, project, title, instructions="", priority=QueueItem.Priority.NORMAL
):
    title = title.strip()
    instructions = instructions.strip()
    item = QueueItem(
        project=project, title=title, instructions=instructions, priority=priority
    )
    item.full_clean()
    item.save()
    return item


def import_issue(*, project, number, title, body, state):
    repository = normalize_repository_url(project.repository_url)
    item, created = QueueItem.objects.get_or_create(
        project=project,
        issue_repository=repository,
        issue_number=number,
        defaults={
            "source": QueueItem.Source.GITHUB,
            "title": title[:200],
            "instructions": body,
            "issue_state": state,
        },
    )
    if created:
        return True
    matching = QueueItem.objects.filter(pk=item.pk)
    matching.update(issue_state=state)
    matching.filter(status=QueueItem.Status.QUEUED).update(
        title=title[:200], instructions=body
    )
    return False


def claimable_items():
    manual = Q(source=QueueItem.Source.MANUAL)
    # Avoid a joined UPDATE so competing claims recheck the item's status.
    autonomous = Project.objects.filter(autonomous=True).values("pk")
    imported = Q(
        source=QueueItem.Source.GITHUB,
        issue_state=QueueItem.IssueState.OPEN,
        project_id__in=autonomous,
    )
    return QueueItem.objects.filter(manual | imported, status=QueueItem.Status.QUEUED)


def claim_next(*, execution_ref=""):
    queued = claimable_items().select_related("project")
    while True:
        item = queued.first()
        if item is None:
            return None
        claimed = claim_item(item, execution_ref=execution_ref)
        if claimed is not None:
            return claimed


def claim_item(item, *, execution_ref=""):
    repository_url = item.repository_url
    base_branch = item.base_branch
    if not repository_url:
        repository_url = normalize_repository_url(item.project.repository_url)
        base_branch = item.project.base_branch

    token = uuid.uuid4()
    queued = claimable_items().filter(pk=item.pk)
    queued = queued.filter(project_id=item.project_id)
    if item.source == QueueItem.Source.GITHUB:
        repository_url = item.repository_url or item.issue_repository
    updated = queued.update(
        status=QueueItem.Status.RUNNING,
        claim_token=token,
        started_at=timezone.now(),
        finished_at=None,
        branch_name=item.branch_name or f"nq/{item.pk}",
        execution_ref=execution_ref,
        repository_url=repository_url,
        base_branch=base_branch,
        result_url="",
        error_summary="",
    )
    if not updated:
        return None
    running = QueueItem.objects.filter(status=QueueItem.Status.RUNNING)
    running = running.select_related("project")
    return running.filter(pk=item.pk, claim_token=token).first()


def finish_item(
    *, item_id, claim_token, status, result_url="", error_summary="", runner_run=None
):
    from runner_control.models import Run

    if status not in (QueueItem.Status.SUCCEEDED, QueueItem.Status.FAILED):
        raise ValidationError({"status": "Choose succeeded or failed."})
    result_field = QueueItem._meta.get_field("result_url")
    try:
        result_field.clean(result_url, None)
        if result_url:
            URLValidator(schemes=["http", "https"])(result_url)
    except ValidationError as exc:
        raise ValidationError({"result_url": exc.messages})
    running = QueueItem.objects.filter(pk=item_id, status=QueueItem.Status.RUNNING)
    claimed = running.filter(claim_token=claim_token)
    owned = Run.objects.filter(item_id=item_id, state=Run.State.ACTIVE)
    if runner_run is None and owned.exists():
        return 0
    if runner_run is not None and not owned.filter(pk=runner_run.pk).exists():
        return 0
    return claimed.update(
        status=status,
        finished_at=timezone.now(),
        result_url=result_url,
        error_summary=error_summary,
    )


@transaction.atomic
def requeue_items(items):
    """Stop the previous workflow before requeuing."""
    from runner_control.models import Run
    from runner_control.services import lock_controller

    lock_controller()
    statuses = [QueueItem.Status.RUNNING, QueueItem.Status.FAILED]
    retryable = items.filter(status__in=statuses)
    item_ids = list(retryable.values_list("pk", flat=True))
    previous = Run.objects.filter(item_id__in=item_ids).exclude(
        state=Run.State.OBSOLETE
    )
    previous.filter(finished_at__isnull=True).update(finished_at=timezone.now())
    previous.update(
        state=Run.State.OBSOLETE, active_slot=None, updated_at=timezone.now()
    )
    return retryable.update(
        status=QueueItem.Status.QUEUED,
        claim_token=None,
        started_at=None,
        finished_at=None,
        execution_ref="",
        result_url="",
        error_summary="",
    )
