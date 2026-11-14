import uuid

from celery.result import EagerResult
from django.contrib import messages
from django.db.models import Min, Q
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST
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
    removed, _ = removable.delete()
    if not removed:
        messages.error(request, "Stop and requeue running work before removing it.")
        return redirect("index")
    messages.success(request, "Work item removed from your queue.")
    return redirect("index")


def log_run_id(request, item):
    selected = request.GET.get("run", "")
    if selected:
        try:
            run_id = uuid.UUID(selected)
        except ValueError:
            raise Http404("Invalid run ID.") from None
        current = item.execution_ref == f"runner:{run_id}"
        if not current and not item.runner_logs.filter(run_id=run_id).exists():
            raise Http404("Run not found for this item.")
        return run_id
    if item.execution_ref.startswith("runner:"):
        try:
            return uuid.UUID(item.execution_ref.removeprefix("runner:"))
        except ValueError:
            pass
    latest = item.runner_logs.order_by("-created_at", "-pk").first()
    return latest.run_id if latest else None


@never_cache
@require_GET
def item_logs(request, pk):
    item = get_object_or_404(QueueItem.objects.select_related("project"), pk=pk)
    run_id = log_run_id(request, item)
    entries = list(item.runner_logs.filter(run_id=run_id)) if run_id else []
    runs = list(
        item.runner_logs.values("run_id")
        .annotate(started_at=Min("created_at"))
        .order_by("-started_at")
    )
    running = (
        item.status == QueueItem.Status.RUNNING
        and item.execution_ref == f"runner:{run_id}"
    )
    context = {
        "item": item,
        "run_id": run_id,
        "runs": runs,
        "entries": entries,
        "running": running,
    }
    context["unlisted_run"] = run_id and not any(
        run["run_id"] == run_id for run in runs
    )
    context["cursor"] = entries[-1].sequence if entries else 0
    return render(request, "work_queue/logs.html", context)


@never_cache
@require_GET
def item_log_data(request, pk):
    item = get_object_or_404(QueueItem, pk=pk)
    run_id = log_run_id(request, item)
    after = request.GET.get("after", "0")
    if not after.isascii() or not after.isdecimal() or len(after) > 4:
        return JsonResponse({"error": "Invalid log cursor."}, status=400)
    entries = item.runner_logs.filter(run_id=run_id, sequence__gt=int(after))
    events = [{"sequence": entry.sequence, "text": entry.text} for entry in entries]
    running = (
        item.status == QueueItem.Status.RUNNING
        and item.execution_ref == f"runner:{run_id}"
    )
    return JsonResponse(
        {"events": events, "running": running, "status": item.get_status_display()}
    )
