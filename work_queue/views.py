from celery.result import EagerResult
from django.contrib import messages
from django.db.models import Q
from django.db.models.deletion import ProtectedError
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST
from django.views.generic import ListView
from kombu.exceptions import OperationalError

from projects.models import Project

from .forms import QueueItemForm
from .models import QueueItem
from .services import claimable_items
from .tasks import import_auto_issues


class Index(ListView):
    def get(self, request, *args, **kwargs):
        return self.render_queue(request, QueueItemForm())

    def render_queue(self, request, form):
        items = QueueItem.objects.select_related("project")
        queue_count = claimable_items().count()
        query = request.GET.get("q", "").strip()
        priority = request.GET.get("priority", "")
        project = request.GET.get("project", "")
        projects = Project.objects.all()
        if not project.isascii() or not project.isdecimal() or len(project) > 18:
            project = ""
        if project and not projects.filter(pk=project).exists():
            project = ""
        if priority not in QueueItem.Priority.values:
            priority = ""
        if query:
            matching_text = Q(title__icontains=query) | Q(instructions__icontains=query)
            items = items.filter(matching_text)
        if priority:
            items = items.filter(priority=priority)
        if project:
            items = items.filter(project_id=project)
        context = {
            "form": form,
            "items": items,
            "queue_count": queue_count,
            "query": query,
            "priority": priority,
            "priorities": QueueItem.Priority.choices,
            "project": project,
            "projects": projects,
            "has_projects": projects.exists(),
            "filtered": bool(query or priority or project),
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
    if item.source == QueueItem.Source.GITHUB:
        messages.error(request, "Imported issues are managed on GitHub.")
        return redirect("index")
    removable = QueueItem.objects.filter(pk=pk).exclude(status=QueueItem.Status.RUNNING)
    try:
        removed, _ = removable.delete()
    except ProtectedError:
        messages.error(
            request, "Work with runner history must be retained for recovery."
        )
        return redirect("index")
    if not removed:
        messages.error(request, "Stop and requeue running work before removing it.")
        return redirect("index")
    messages.success(request, "Work item removed from your queue.")
    return redirect("index")
