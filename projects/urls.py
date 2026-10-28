from django.urls import path

from . import api

urlpatterns = [
    path("api/v1/projects/", api.projects, name="api_projects"),
]
