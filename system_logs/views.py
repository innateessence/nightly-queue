from urllib.parse import urlencode

from django.core.paginator import Paginator
from django.db.models import Q
from django.shortcuts import render
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

from .handlers import MAX_ENTRIES, RETENTION_DAYS, retention_cutoff
from .models import SystemLog


@never_cache
@require_GET
def system_logs(request):
    entries = SystemLog.objects.filter(created_at__gte=retention_cutoff())
    query = request.GET.get("q", "").strip()[:200]
    level = request.GET.get("level", "")
    source = request.GET.get("source", "")[:120]
    if level not in SystemLog.Level.values:
        level = ""
    sources = entries.order_by("source").values_list("source", flat=True).distinct()
    if query:
        entries = entries.filter(
            Q(message__icontains=query) | Q(details__icontains=query)
        )
    if level:
        entries = entries.filter(level=level)
    if source:
        entries = entries.filter(source=source)
    filters = {
        key: value
        for key, value in {"q": query, "level": level, "source": source}.items()
        if value
    }
    params = urlencode(filters)
    page_number = request.GET.get("page", "1")
    if not page_number.isascii() or not page_number.isdecimal() or len(page_number) > 9:
        page_number = "1"
    page = Paginator(entries, 50).get_page(page_number)
    categories = [
        {"label": "System Logs", "url": reverse("system_logs"), "active": True},
        {"label": "Runner Logs", "url": None, "active": False},
    ]
    context = {
        "page": page,
        "query": query,
        "level": level,
        "source": source,
        "levels": SystemLog.Level.choices,
        "sources": sources,
        "categories": categories,
        "filters_query": params,
        "page_query": params + "&" if params else "",
        "filtered": bool(filters),
        "retention_days": RETENTION_DAYS,
        "max_entries": MAX_ENTRIES,
    }
    response = render(request, "system_logs/index.html", context)
    response["Cache-Control"] = "no-store"
    return response
