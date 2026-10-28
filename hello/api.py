import json
import uuid
from functools import wraps

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.http import JsonResponse
from django.utils.crypto import constant_time_compare
from django.views.decorators.csrf import csrf_exempt

from .forms import QueueItemForm
from .models import Project, QueueItem
from .services import claim_next, finish_item


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


def item_data(item, *, include_claim=False):
    data = {
        "id": item.pk,
        "project": project_data(item.project),
        "title": item.title,
        "instructions": item.instructions,
        "priority": item.priority,
        "status": item.status,
        "created_at": item.created_at.isoformat(),
        "started_at": item.started_at.isoformat() if item.started_at else None,
        "finished_at": item.finished_at.isoformat() if item.finished_at else None,
        "branch_name": item.branch_name,
        "execution_ref": item.execution_ref,
        "result_url": item.result_url,
        "error_summary": item.error_summary,
    }
    if not include_claim:
        return data
    data["claim_token"] = str(item.claim_token)
    data["repository_url"] = item.repository_url
    data["base_branch"] = item.base_branch
    return data


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


@api_view("POST")
def claim(request):
    data = read_body(request, ["execution_ref"])
    execution_ref = string_field(data, "execution_ref", max_length=200)
    item = claim_next(execution_ref=execution_ref)
    if item is None:
        return JsonResponse({"item": None})
    return JsonResponse({"item": item_data(item, include_claim=True)})


@api_view("POST")
def finish(request, pk):
    data = read_body(request, ["claim_token", "status", "result_url", "error_summary"])
    claim_token = string_field(data, "claim_token")
    try:
        token = uuid.UUID(claim_token)
    except ValueError:
        message = "Send the claim token returned when claiming work."
        raise ValidationError({"claim_token": message})
    if not QueueItem.objects.filter(pk=pk).exists():
        return JsonResponse({"error": "Item not found."}, status=404)
    result = {
        "status": string_field(data, "status"),
        "result_url": string_field(data, "result_url", max_length=500),
        "error_summary": string_field(data, "error_summary"),
    }
    with transaction.atomic():
        updated = finish_item(item_id=pk, claim_token=token, **result)
        if not updated:
            message = "Item is not running under this claim."
            return JsonResponse({"error": message}, status=409)
        item = QueueItem.objects.select_related("project").get(pk=pk)
        return JsonResponse({"item": item_data(item)})
