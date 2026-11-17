from django.urls import path

from . import api, views
from runner_control import deletion_views

urlpatterns = [
    path("projects/<int:pk>/delete/", deletion_views.project, name="delete_project"),
    path("projects/", views.project_list, name="project_list"),
    path("projects/<int:pk>/edit/", views.edit_project, name="edit_project"),
    path("api/v1/projects/", api.projects, name="api_projects"),
]
