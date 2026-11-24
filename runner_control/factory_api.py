from django.core.exceptions import ValidationError
from django.http import JsonResponse
from django.shortcuts import get_object_or_404

from vibearr_app.api import string_field
from vibearr_runner.factory_contract import MAX_ARTIFACT_BYTES

from .api import run_response
from .auth import runner_api_view
from .factory_services import save_artifact
from .plan_api import document_body, request_bearer
from .plan_presentation import present_payload
from .services import owned_run, redact_payload


def artifact_data(entry, *, include_document=True, bearer=""):
    result = {
        "id": str(entry.pk),
        "operation_id": str(entry.operation_id),
        "digest": entry.digest,
        "reason": entry.reason,
        "created_at": entry.created_at.isoformat(),
        "sequence": entry.sequence,
        "role": entry.role,
        "round": entry.round,
        "context_id": str(entry.context_id),
        "specification_id": (
            str(entry.specification_id) if entry.specification_id else None
        ),
    }
    if include_document:
        result["document"] = entry.document
    safe, error = present_payload(entry.run, result, bearer=bearer)
    if error:
        return {
            "document": None,
            "reason": error,
            "presentation_only": True,
            "unavailable": True,
        }
    safe["redacted"] = safe != result
    safe["presentation_only"] = include_document and safe["document"] != entry.document
    safe["unavailable"] = False
    return safe


@runner_api_view("GET", "POST")
def artifacts(request, run_id):
    if request.method == "POST":
        data = document_body(
            request,
            ["claim_token", "operation_id", "document", "reason"],
            MAX_ARTIFACT_BYTES + 4096,
        )
        control_secrets = [
            request.headers.get("X-Vibearr-Controller-Secret", ""),
            request.headers.get("X-NQ-Controller-Secret", ""),
        ]
        supplied = [data.get("document"), data.get("reason", "")]
        if redact_payload(supplied, control_secrets) != supplied:
            raise ValidationError(
                {"document": "Factory artifacts cannot contain controller secrets."}
            )

        def write():
            entry = save_artifact(
                runner=request.runner_identity,
                run_id=run_id,
                claim_token=data["claim_token"],
                operation_id=data["operation_id"],
                document=data.get("document"),
                reason=string_field(data, "reason", max_length=2000),
            )
            return JsonResponse(
                {"artifact": artifact_data(entry, bearer=request_bearer(request))}
            )

        return run_response(write)

    def read():
        run = owned_run(request.runner_identity, run_id)
        try:
            after = int(request.GET.get("after", "0"))
            if not 0 <= after < 2**31:
                raise ValueError
        except ValueError:
            raise ValidationError(
                {"after": "Send a nonnegative artifact cursor."}
            ) from None
        entries = list(
            run.factory_artifacts.filter(sequence__gt=after).order_by("sequence")[:11]
        )
        more = len(entries) > 10
        entries = entries[:10]
        return JsonResponse(
            {
                "artifacts": [
                    artifact_data(
                        entry, include_document=False, bearer=request_bearer(request)
                    )
                    for entry in entries
                ],
                "has_more": more,
                "next_after": entries[-1].sequence if more else None,
            }
        )

    return run_response(read)


@runner_api_view("GET")
def detail(request, run_id, artifact_id):
    def read():
        run = owned_run(request.runner_identity, run_id)
        entry = get_object_or_404(run.factory_artifacts, pk=artifact_id)
        return JsonResponse(
            {"artifact": artifact_data(entry, bearer=request_bearer(request))}
        )

    return run_response(read)
