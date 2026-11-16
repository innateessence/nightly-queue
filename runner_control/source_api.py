from django.db import transaction
from django.http import JsonResponse
from django.views.decorators.debug import sensitive_variables

from vibearr_app.api import read_body
from work_queue.github import GitHubAPIError
from work_queue.services import refresh_issue_for_launch

from .api import run_response
from .auth import runner_api_view
from .secrets import secret_document
from .services import lock_controller, owned_run, uuid_field, verify_active


@runner_api_view("POST")
@sensitive_variables()
def refresh_source(request, run_id):
    data = read_body(request, ["claim_token"])
    claim_token = uuid_field(data, "claim_token")

    def operation():
        with transaction.atomic():
            lock_controller(request.runner_identity)
            run = owned_run(request.runner_identity, run_id)
            verify_active(run, claim_token)
        token = ""
        if run.item.source == "github":
            selection = dict(
                scope="run",
                run_id=str(run_id),
                claim_token=str(claim_token),
                purpose="issue",
            )
            token = secret_document(selection, request.runner_identity)["material"][
                "secret"
            ]
        try:
            snapshot = refresh_issue_for_launch(
                item_id=run.item_id, claim_token=claim_token, token=token
            )
        except GitHubAPIError:
            return JsonResponse(
                {"error": "Fresh GitHub context is unavailable; wait before launch."},
                status=503,
            )
        with transaction.atomic():
            lock_controller(request.runner_identity)
            verify_active(owned_run(request.runner_identity, run_id), claim_token)
        return JsonResponse({"source_snapshot": snapshot})

    return run_response(operation)
