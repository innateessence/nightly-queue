import json
from functools import wraps

from django.conf import settings
from django.core.exceptions import ValidationError
from django.http import JsonResponse
from django.utils.crypto import constant_time_compare
from django.views.decorators.csrf import csrf_exempt

from .forms import QueueItemForm
from .models import Project, QueueItem


def authentication_error(request):
    token = settings.NQ_API_TOKEN
    if not token:
        message = "API disabled: configure NQ_API_TOKEN."
        return JsonResponse({"error": message}, status=503)
    authorization = request.headers.get("Authorization", "").split()
    if len(authorization) != 2:
        return unauthorized()
    scheme, supplied_token = authorization
    if scheme.lower() != "bearer":
        return unauthorized()
    if not constant_time_compare(supplied_token, token):
        return unauthorized()
    return None


def unauthorized():
    message = "A valid bearer token is required."
    response = JsonResponse({"error": message}, status=401)
    response["WWW-Authenticate"] = "Bearer"
    return response


def api_view(method):
    def decorate(view):
        @csrf_exempt
        @wraps(view)
        def wrapped(request, *args, **kwargs):
            error = authentication_error(request)
            if error is not None:
                return error
            if request.method != method:
                response = JsonResponse({"error": f"Use {method}."}, status=405)
                response["Allow"] = method
                return response
            try:
                return view(request, *args, **kwargs)
            except ValidationError as exc:
                errors = {"__all__": exc.messages}
                if hasattr(exc, "error_dict"):
                    errors = exc.message_dict
                return JsonResponse({"errors": errors}, status=400)

        return wrapped

    return decorate


def read_body(request, allowed_fields):
    if request.content_type != "application/json":
        raise ValidationError({"body": "Send an application/json object."})
    try:
        data = json.loads(request.body)
    except (ValueError, UnicodeDecodeError):
        raise ValidationError({"body": "Send valid JSON."})
    if not isinstance(data, dict):
        raise ValidationError({"body": "Send a JSON object."})
    unknown = set(data) - set(allowed_fields)
    if unknown:
        fields = ", ".join(sorted(unknown))
        raise ValidationError({"body": f"Unknown fields: {fields}."})
    return data


def string_field(data, field, default="", max_length=None):
    value = data.get(field, default)
    if not isinstance(value, str):
        raise ValidationError({field: "Send a string."})
    if max_length and len(value) > max_length:
        raise ValidationError({field: f"Use at most {max_length} characters."})
    return value


def project_data(project):
    return {
        "id": project.pk,
        "name": project.name,
        "repository_url": project.repository_url,
        "base_branch": project.base_branch,
    }


def item_data(item):
    return {
        "id": item.pk,
        "project": project_data(item.project),
        "title": item.title,
        "instructions": item.instructions,
        "priority": item.priority,
        "created_at": item.created_at.isoformat(),
    }


@api_view("GET")
def projects(request):
    data = [project_data(project) for project in Project.objects.all()]
    return JsonResponse({"projects": data})


@api_view("POST")
def enqueue(request):
    data = read_body(request, ["project_id", "title", "instructions", "priority"])
    project_id = data.get("project_id")
    if type(project_id) is not int or not 0 < project_id < 2**63:
        message = "Send the numeric ID of an existing project."
        raise ValidationError({"project_id": message})
    fields = {
        "project": project_id,
        "title": string_field(data, "title"),
        "instructions": string_field(data, "instructions"),
        "priority": string_field(data, "priority", QueueItem.Priority.NORMAL),
    }
    form = QueueItemForm(fields)
    if form.is_valid():
        item = form.save()
        return JsonResponse({"item": item_data(item)}, status=201)
    errors = dict(form.errors)
    if "project" in errors:
        errors["project_id"] = errors.pop("project")
    return JsonResponse({"errors": errors}, status=400)
