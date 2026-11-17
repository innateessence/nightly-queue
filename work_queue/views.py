from celery.result import EagerResult
from urllib.parse import urlencode

from django.contrib import messages
from django.db.models import Count, OuterRef, Q, Subquery
from django.core.exceptions import ValidationError
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST
from django.views.generic import ListView
from kombu.exceptions import OperationalError

from projects.models import Project
from runner_control.models import Run

from .forms import QueueItemForm
from .models import QueueItem
from .services import claimable_items, source_wait_reason, delete_queue_item
from .tasks import import_auto_issues


class Index(ListView):
    def get(self, request, *args, **kwargs):
        return self.render_queue(request, QueueItemForm())

    def render_queue(self, request, form):
        items = QueueItem.objects.select_related("project")
        queue_count = claimable_items().count()
        query = request.GET.get("q", "").strip()
        priority = request.GET.get("priority", "")
        status = request.GET.get("status", "")
        project = request.GET.get("project", "")
        projects = Project.objects.all()
        if not project.isascii() or not project.isdecimal() or len(project) > 18:
            project = ""
        if project and not projects.filter(pk=project).exists():
            project = ""
        if priority not in QueueItem.Priority.values:
            priority = ""
        if status not in QueueItem.Status.values:
            status = ""
        if query:
            matching_text = Q(title__icontains=query) | Q(instructions__icontains=query)
            items = items.filter(matching_text)
        if priority:
            items = items.filter(priority=priority)
        if project:
            items = items.filter(project_id=project)
        counts = dict(items.values_list("status").annotate(total=Count("pk")))
        filters = {"q": query, "priority": priority, "project": project}
        filters = {key: value for key, value in filters.items() if value}
        status_options = [("", "All work"), *QueueItem.Status.choices]
        status_links = []
        for value, label in status_options:
            params = {**filters, "status": value} if value else filters
            status_links.append(
                {
                    "value": value,
                    "label": label,
                    "query": urlencode(params),
                    "count": counts.get(value, 0) if value else sum(counts.values()),
                }
            )
        if status:
            items = items.filter(status=status)
        latest_run = Run.objects.filter(item_id=OuterRef("pk")).order_by(
            "-created_at", "-pk"
        )
        items = items.annotate(latest_run_id=Subquery(latest_run.values("pk")[:1]))
        items = list(items)
        for item in items:
            item.waiting_reason = (
                source_wait_reason(item)
                if item.status == QueueItem.Status.QUEUED
                else ""
            )
        context = {
            "queue_total": QueueItem.objects.filter(
                status=QueueItem.Status.QUEUED
            ).count(),
            "form": form,
            "items": items,
            "queue_count": queue_count,
            "query": query,
            "priority": priority,
            "status": status,
            "statuses": QueueItem.Status.choices,
            "status_links": status_links,
            "clear_filters_query": urlencode({"status": status}) if status else "",
            "priorities": QueueItem.Priority.choices,
            "project": project,
            "projects": projects,
            "has_projects": projects.exists(),
            "filtered": bool(query or priority or project or status),
        }
        return render(request, "work_queue/index.html", context)

    def post(self, request, *args, **kwargs):
        form = QueueItemForm(request.POST)
        if not form.is_valid():
            return self.render_queue(request, form)
        form.save()
        messages.success(request, "Work item added to your queue.")
        return redirect("index")


@require_POST
def sync_issues(request):
    try:
        result = import_auto_issues.delay()
    except OperationalError:
        messages.error(
            request,
            "Unable to start issue sync. Please try again shortly.",
        )
        return redirect("index")
    if not isinstance(result, EagerResult):
        messages.success(
            request,
            "Issue sync started for all autonomous projects. Refresh the queue shortly to see new items.",
        )
        return redirect("index")
    if not result.successful():
        messages.error(
            request, "Issue sync failed. Please try again or check the server logs."
        )
        return redirect("index")
    summary = result.result
    if summary["skipped"]:
        messages.error(
            request,
            "Issue sync skipped. Check the GitHub API credential in Settings and try again.",
        )
        return redirect("index")
    text = f"Issue sync finished. Projects synced: {summary['projects']}. New items: {summary['created']}."
    if summary["failed"]:
        messages.warning(
            request,
            f"{text} Projects that could not be synced: {summary['failed']}. Check GitHub access and try again.",
        )
    else:
        messages.success(request, text)
    return redirect("index")


@require_POST
def remove_item(request, pk):
    item = get_object_or_404(QueueItem, pk=pk)
    try:
        delete_queue_item(item)
    except ValidationError:
        messages.error(request, "Stop and requeue running work before removing it.")
        return redirect("index")
    messages.success(request, "Work item removed from your queue.")
    return redirect("index")
