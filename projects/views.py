from copy import copy
from itertools import chain

from django.contrib import messages
from django.db import transaction

from runner_control.configuration import configuration_document
from runner_control.plan_presentation import (
    form_presentation_error,
    present_configuration_payload,
)
from runner_control.settings_forms import ProjectRunnerSettingsForm
from runner_control.settings_models import ProjectRunnerSettings
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_http_methods

from .forms import ProjectForm
from .models import Project


def present_project(project, configuration):
    safe = copy(project)
    values = [project.name, project.repository_url, project.base_branch]
    data, error = present_configuration_payload(configuration, values)
    safe.name = data[0] if data is not None else "Project content unavailable"
    safe.repository_url = data[1] if data is not None else ""
    safe.base_branch = data[2] if data is not None else ""
    safe.presentation_error = error
    return safe


def present_form_errors(form, runner_form, configuration):
    fields = [*form.errors.values(), *runner_form.errors.values()]
    errors = [str(error) for error in chain.from_iterable(fields)]
    safe, error = present_configuration_payload(configuration, errors)
    return safe if safe is not None else []


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
    configuration = configuration_document()
    form_error = form_presentation_error(
        form, configuration
    ) or form_presentation_error(runner_form, configuration)
    projects = [
        present_project(project, configuration) for project in Project.objects.all()
    ]
    context = {
        "form": form,
        "runner_form": runner_form,
        "projects": projects,
        "form_unavailable": form_error,
        "form_presentation_errors": (
            present_form_errors(form, runner_form, configuration) if form_error else []
        ),
    }
    response = render(request, "projects/index.html", context)
    response["Cache-Control"] = "no-store"
    return response


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
    configuration = configuration_document()
    effective = next(
        entry
        for entry in configuration["projects"]
        if entry["project_id"] == project.pk
    )
    safe, error = present_configuration_payload(
        configuration, effective, preserve_secret_versions=True
    )
    effective = safe if safe is not None else {"effective": {}, "errors": []}
    form_error = form_presentation_error(
        form, configuration
    ) or form_presentation_error(runner_form, configuration)
    display = [
        {"label": field.replace("_id", "").replace("_", " ").title(), "value": value}
        for field, value in effective.get("effective", {}).items()
    ]
    context = {
        "form": form,
        "runner_form": runner_form,
        "project": present_project(project, configuration),
        "form_unavailable": form_error,
        "form_presentation_errors": (
            present_form_errors(form, runner_form, configuration) if form_error else []
        ),
        "effective_unavailable": error,
        "effective_configuration": effective,
        "effective_display": display,
    }
    response = render(request, "projects/edit.html", context)
    response["Cache-Control"] = "no-store"
    return response
