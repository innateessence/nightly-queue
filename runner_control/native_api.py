from django.http import JsonResponse

from .api import bounded_data, response_data, run_response
from .auth import runner_api_view
from .native_services import candidates, update_status
from .services import uuid_field


@runner_api_view("POST")
def delivery_recovery(request, run_id):
    from .delivery_recovery import recover_delivery
    from vibearr_app.api import read_body, string_field

    data = read_body(
        request,
        [
            "operation_id",
            "expected_finish_digest",
            "expected_publication_id",
            "expected_head_sha",
        ],
    )
    operation_id = uuid_field(data, "operation_id")
    expected = {
        key: string_field(data, key, max_length=64)
        for key in (
            "expected_finish_digest",
            "expected_publication_id",
            "expected_head_sha",
        )
    }

    def operation():
        run, recovery = recover_delivery(
            runner=request.runner_identity,
            run_id=run_id,
            operation_id=operation_id,
            expected=expected,
        )
        response = response_data(run, include_claim=run.state == "active")
        response["delivery_recovery"] = {
            "generation": 1,
            "operation_id": str(recovery.operation_id),
            "archive": recovery.archive,
            "native": recovery.resume,
        }
        return JsonResponse(response)

    return run_response(operation)


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
