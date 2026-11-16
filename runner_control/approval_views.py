from django.core.exceptions import ValidationError
from django.http import Http404, HttpResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_GET, require_POST

from .approvals import decide_approval
from .models import ActionApproval
from .services import RunConflict, RunNotFound


@require_GET
def inbox(request):
    approvals = ActionApproval.objects.select_related("run__item").order_by(
        "-created_at"
    )[:100]
    response = render(
        request, "runner_control/approvals.html", {"approvals": approvals}
    )
    response["Cache-Control"] = "no-store"
    return response


@require_POST
def decide(request, approval_id):
    try:
        decide_approval(
            approval_id,
            request.POST.get("decision", ""),
            request.POST.get("binding_digest", ""),
            request.POST.get("note", ""),
        )
    except RunNotFound:
        raise Http404 from None
    except (RunConflict, ValidationError) as exc:
        return HttpResponse(str(exc), status=409, content_type="text/plain")
    return redirect("runner_approval_inbox")
