from django.contrib import messages
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST
from django.views.generic import ListView

from .forms import QueueItemForm
from .models import Project, QueueItem


class Index(ListView):
    def get(self, request, *args, **kwargs):
        return self.render_queue(request, QueueItemForm())

    def render_queue(self, request, form):
        items = QueueItem.objects.select_related("project")
        queue_count = items.filter(status=QueueItem.Status.QUEUED).count()
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
        return render(request, "hello/index.html", context)

    def post(self, request, *args, **kwargs):
        form = QueueItemForm(request.POST)
        if not form.is_valid():
            return self.render_queue(request, form)
        form.save()
        messages.success(request, "Work item added to your queue.")
        return redirect("index")


@require_POST
def remove_item(request, pk):
    get_object_or_404(QueueItem, pk=pk)
    removable = QueueItem.objects.filter(pk=pk).exclude(status=QueueItem.Status.RUNNING)
    removed, _ = removable.delete()
    if not removed:
        messages.error(request, "Stop and requeue running work before removing it.")
        return redirect("index")
    messages.success(request, "Work item removed from your queue.")
    return redirect("index")
