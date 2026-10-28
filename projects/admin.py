from django.contrib import admin

from .models import Project


@admin.register(Project)
class ProjectAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "repository_url", "base_branch")
    search_fields = ("name", "repository_url")
    readonly_fields = ("id", "created_at", "updated_at")
