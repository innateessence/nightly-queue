import json
from functools import wraps

from django.conf import settings
from django.core.exceptions import ValidationError
from django.http import JsonResponse
from django.utils.crypto import constant_time_compare
from django.views.decorators.csrf import csrf_exempt


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
