from django.contrib import admin

from .models import QueueItem
from .services import requeue_items


@admin.register(QueueItem)
class QueueItemAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "title",
        "project",
        "source",
        "status",
        "priority",
        "created_at",
    )
    list_filter = ("source", "project", "status", "priority")
    search_fields = ("title", "instructions", "project__name")
    list_select_related = ("project",)
    readonly_fields = (
        "source",
        "issue_number",
        "issue_repository",
        "issue_state",
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
        description="Requeue failed/interrupted work (stop its runner first)",
    )
    def requeue_stopped_work(self, request, queryset):
        result = requeue_items(queryset, return_summary=True)
        message = (
            f"Requeued {result['requeued']} item(s); previous claim tokens are invalid."
        )
        if result["pending"]:
            message += f" Waiting for termination: {result['pending']}."
        if result["held"]:
            message += f" Held for delivery reconciliation: {result['held']}."
        self.message_user(request, message)
