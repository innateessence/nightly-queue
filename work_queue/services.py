import uuid

from django.core.exceptions import ValidationError
from django.core.validators import URLValidator
from django.utils import timezone

from projects.validators import normalize_repository_url

from .models import QueueItem


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


def claim_next(*, execution_ref=""):
    queued = QueueItem.objects.filter(status=QueueItem.Status.QUEUED)
    queued = queued.select_related("project")
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
    queued = QueueItem.objects.filter(pk=item.pk, status=QueueItem.Status.QUEUED)
    queued = queued.filter(project_id=item.project_id)
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


def finish_item(*, item_id, claim_token, status, result_url="", error_summary=""):
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
    return claimed.update(
        status=status,
        finished_at=timezone.now(),
        result_url=result_url,
        error_summary=error_summary,
    )


def requeue_items(items):
    """Stop the previous workflow before requeuing."""
    statuses = [QueueItem.Status.RUNNING, QueueItem.Status.FAILED]
    retryable = items.filter(status__in=statuses)
    return retryable.update(
        status=QueueItem.Status.QUEUED,
        claim_token=None,
        started_at=None,
        finished_at=None,
        execution_ref="",
        result_url="",
        error_summary="",
    )
