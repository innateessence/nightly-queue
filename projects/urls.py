from django.urls import path

from . import api, views

urlpatterns = [
    path("projects/", views.project_list, name="project_list"),
    path("projects/<int:pk>/edit/", views.edit_project, name="edit_project"),
    path("api/v1/projects/", api.projects, name="api_projects"),
]
