from django.http import JsonResponse

from vibearr_app.api import api_view

from .models import Project


def project_data(project):
    return {
        "id": project.pk,
        "name": project.name,
        "repository_url": project.repository_url,
        "base_branch": project.base_branch,
        "autonomous": project.autonomous,
    }


@api_view("GET")
def projects(request):
    data = [project_data(project) for project in Project.objects.all()]
    return JsonResponse({"projects": data})
