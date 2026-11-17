from django.http import JsonResponse

from .auth import runner_api_view
from .configuration import configuration_document


@runner_api_view("GET")
def configuration(request):
    return JsonResponse(configuration_document(request.runner_identity))
