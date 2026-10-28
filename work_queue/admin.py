from django.contrib import admin

from .models import QueueItem
from .services import requeue_items


@admin.register(QueueItem)
class QueueItemAdmin(admin.ModelAdmin):
    list_display = ("id", "title", "project", "status", "priority", "created_at")
    list_filter = ("project", "status", "priority")
    search_fields = ("title", "instructions", "project__name")
    list_select_related = ("project",)
    readonly_fields = (
        "created_at",
        "status",
        "claim_token",
        "started_at",
        "finished_at",
        "branch_name",
        "execution_ref",
        "result_url",
        "error_summary",
        "repository_url",
        "base_branch",
    )
    actions = ["requeue_stopped_work"]

    def get_readonly_fields(self, request, obj=None):
        fields = super().get_readonly_fields(request, obj)
        if obj is None:
            return fields
        if obj.status == QueueItem.Status.QUEUED and not obj.repository_url:
            return fields
        return (*fields, "project", "title", "instructions", "priority")

    def has_delete_permission(self, request, obj=None):
        if obj and obj.status == QueueItem.Status.RUNNING:
            return False
        return super().has_delete_permission(request, obj)

    def delete_queryset(self, request, queryset):
        queryset.exclude(status=QueueItem.Status.RUNNING).delete()

    @admin.action(
        permissions=["change"],
        description="Requeue failed/interrupted work (stop its n8n run first)",
    )
    def requeue_stopped_work(self, request, queryset):
        count = requeue_items(queryset)
        message = f"Requeued {count} item(s); previous claim tokens are invalid."
        self.message_user(request, message)
