from django.http import JsonResponse

from .api import bounded_data, response_data, run_response
from .auth import runner_api_view
from .native_services import candidates, update_status
from .services import uuid_field


@runner_api_view("GET")
def native_candidates(request):
    return run_response(lambda: JsonResponse(candidates(request.runner_identity)))


@runner_api_view("POST")
def native_status(request, run_id):
    data = bounded_data(
        request,
        [
            "claim_token",
            "sequence",
            "stage",
            "wait_reason",
            "detail",
            "repair_count",
            "validation_status",
            "session_id",
        ],
    )
    claim_token = uuid_field(data, "claim_token")
    values = {key: value for key, value in data.items() if key != "claim_token"}
    return run_response(
        lambda: JsonResponse(
            response_data(
                update_status(
                    runner=request.runner_identity,
                    run_id=run_id,
                    claim_token=claim_token,
                    data=values,
                )
            )
        )
    )
