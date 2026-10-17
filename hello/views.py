from django.contrib import messages
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST, require_http_methods

from .forms import QueueItemForm
from .models import QueueItem


@require_http_methods(["GET", "POST"])
def index(request):
    form = QueueItemForm(request.POST if request.method == "POST" else None)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Work item added to your queue.")
        return redirect("index")

    items = QueueItem.objects.all()
    queue_count = items.count()
    query = request.GET.get("q", "").strip()
    priority = request.GET.get("priority", "")
    if priority not in QueueItem.Priority.values:
        priority = ""
    if query:
        items = items.filter(
            Q(title__icontains=query) | Q(instructions__icontains=query)
        )
    if priority:
        items = items.filter(priority=priority)
    return render(
        request,
        "hello/index.html",
        {
            "form": form,
            "items": items,
            "queue_count": queue_count,
            "query": query,
            "priority": priority,
            "priorities": QueueItem.Priority.choices,
            "filtered": bool(query or priority),
        },
    )


@require_POST
def remove_item(request, pk):
    item = get_object_or_404(QueueItem, pk=pk)
    item.delete()
    messages.success(request, "Work item removed from your queue.")
    return redirect("index")
