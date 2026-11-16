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
from .services import RunConflict
from projects.models import Project


@runner_api_view("POST")
@sensitive_variables()
def refresh_source(request, run_id):
    data = read_body(request, ["claim_token", "mode"])
    claim_token = uuid_field(data, "claim_token")
    mode = data.get("mode")
    if mode is not None and (
        not isinstance(mode, str) or mode not in {"initial", "continuation"}
    ):
        return JsonResponse(
            {"error": "Choose initial or continuation source mode."}, status=400
        )

    def operation():
        with transaction.atomic():
            lock_controller(request.runner_identity)
            run = owned_run(request.runner_identity, run_id)
            verify_active(run, claim_token)
        token = ""
        if run.item.source == "github" or mode is not None:
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
                item_id=run.item_id,
                claim_token=claim_token,
                token=token,
                runner=request.runner_identity,
                run_id=run_id,
                mode=mode,
            )
        except GitHubAPIError:
            return JsonResponse(
                {"error": "Fresh GitHub context is unavailable; wait before launch."},
                status=503,
            )
        if mode is None:
            return JsonResponse({"source_snapshot": snapshot})
        project = Project.objects.get(pk=run.item.project_id)
        if snapshot.get("repository_id") != project.github_repository_id:
            raise RunConflict("Repository authority changed after source refresh.")
        return JsonResponse(
            {
                "source_snapshot": snapshot,
                "repository": {
                    "repository_id": project.github_repository_id,
                    "owner_id": project.github_owner_id,
                    "repository_url": project.repository_url,
                    "default_branch": project.github_default_branch,
                    "authority_version": project.github_authority_version,
                },
                "eligible": True,
                "reason": "",
            }
        )

    return run_response(operation)
