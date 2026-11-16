import hashlib
from functools import wraps

from django.core.exceptions import ValidationError
from django.http import JsonResponse
from django.utils.crypto import constant_time_compare
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.debug import sensitive_variables

from nightly_queue.api import unauthorized

from .models import RunnerIdentity


def token_digest(token):
    return hashlib.sha256(token.encode()).hexdigest()


@sensitive_variables()
def runner_authentication(request):
    authorization = request.headers.get("Authorization", "").split()
    if len(authorization) != 2 or authorization[0].lower() != "bearer":
        return None
    digest = token_digest(authorization[1])
    for runner in RunnerIdentity.objects.filter(enabled=True):
        if constant_time_compare(runner.token_digest, digest):
            return runner
    return None


def runner_api_view(*methods):
    def decorate(view):
        @csrf_exempt
        @wraps(view)
        def wrapped(request, *args, **kwargs):
            runner = runner_authentication(request)
            if runner is None:
                return unauthorized()
            proof = request.headers.get("X-NQ-Controller-Secret", "")
            protected = view.__name__ not in {
                "identity",
                "configuration",
                "active",
                "gates",
                "bind_controller",
            }
            runner.controller_proven = bool(proof) and constant_time_compare(
                runner.controller_digest, token_digest(proof)
            )
            if protected and request.method in methods and not runner.controller_proven:
                response = JsonResponse(
                    {"error": "Controller volume proof is required."}, status=409
                )
                response["Cache-Control"] = "no-store"
                return response
            request.runner_identity = runner
            if request.method not in methods:
                response = JsonResponse(
                    {"error": f"Use {', '.join(methods)}."}, status=405
                )
                response["Allow"] = ", ".join(methods)
            else:
                try:
                    response = view(request, *args, **kwargs)
                except ValidationError as exc:
                    errors = (
                        exc.message_dict
                        if hasattr(exc, "error_dict")
                        else {"__all__": exc.messages}
                    )
                    response = JsonResponse({"errors": errors}, status=400)
            response["Cache-Control"] = "no-store"
            return response

        return wrapped

    return decorate
