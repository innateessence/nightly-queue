from django.contrib import messages
from django.db import transaction

from runner_control.configuration import effective_project_configuration
from runner_control.settings_forms import ProjectRunnerSettingsForm
from runner_control.settings_models import ProjectRunnerSettings
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_http_methods

from .forms import ProjectForm
from .models import Project


@require_http_methods(["GET", "POST"])
def project_list(request):
    form = ProjectForm(request.POST if request.method == "POST" else None)
    runner_form = ProjectRunnerSettingsForm(
        request.POST if request.method == "POST" else None, prefix="runner"
    )
    if request.method == "POST" and form.is_valid() and runner_form.is_valid():
        with transaction.atomic():
            project = form.save()
            runner_form.save(project)
        messages.success(request, "Project created. You can now add work to it.")
        return redirect("project_list")
    context = {
        "form": form,
        "runner_form": runner_form,
        "projects": Project.objects.all(),
    }
    return render(request, "projects/index.html", context)


@require_http_methods(["GET", "POST"])
def edit_project(request, pk):
    project = get_object_or_404(Project, pk=pk)
    data = request.POST if request.method == "POST" else None
    form = ProjectForm(data, instance=project)
    saved = ProjectRunnerSettings.objects.filter(project=project).first()
    runner_form = ProjectRunnerSettingsForm(data, instance=saved, prefix="runner")
    if request.method == "POST" and form.is_valid() and runner_form.is_valid():
        with transaction.atomic():
            form.save()
            runner_form.save(project)
        messages.success(request, "Project updated.")
        return redirect("project_list")
    effective = effective_project_configuration(project)
    display = [
        {"label": field.replace("_id", "").replace("_", " ").title(), "value": value}
        for field, value in effective["effective"].items()
    ]
    context = {
        "form": form,
        "runner_form": runner_form,
        "project": project,
        "effective_configuration": effective,
        "effective_display": display,
    }
    return render(request, "projects/edit.html", context)
