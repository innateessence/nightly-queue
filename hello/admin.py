from django.contrib import admin

from .models import Project, QueueItem


@admin.register(Project)
class ProjectAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "repository_url", "base_branch")
    search_fields = ("name", "repository_url")
    readonly_fields = ("id", "created_at", "updated_at")


@admin.register(QueueItem)
class QueueItemAdmin(admin.ModelAdmin):
    list_display = ("id", "title", "project", "priority", "created_at")
    list_filter = ("project", "priority")
    search_fields = ("title", "instructions", "project__name")
    list_select_related = ("project",)
    readonly_fields = ("created_at",)
