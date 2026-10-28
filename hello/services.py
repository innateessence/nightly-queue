from .models import Project, QueueItem


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


def backfill_projects(*, project_id, item_ids=None):
    project = Project.objects.get(pk=project_id)
    items = QueueItem.objects.filter(project__isnull=True)
    if item_ids is not None:
        items = items.filter(pk__in=item_ids)
    return items.update(project=project)
