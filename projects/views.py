from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_http_methods

from .forms import ProjectForm
from .models import Project


@require_http_methods(["GET", "POST"])
def project_list(request):
    form = ProjectForm(request.POST if request.method == "POST" else None)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Project created. You can now add work to it.")
        return redirect("project_list")
    context = {"form": form, "projects": Project.objects.all()}
    return render(request, "projects/index.html", context)


@require_http_methods(["GET", "POST"])
def edit_project(request, pk):
    project = get_object_or_404(Project, pk=pk)
    data = request.POST if request.method == "POST" else None
    form = ProjectForm(data, instance=project)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Project updated.")
        return redirect("project_list")
    return render(request, "projects/edit.html", {"form": form, "project": project})
