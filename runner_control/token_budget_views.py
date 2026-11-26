from django.contrib import messages
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_http_methods

from .plan_presentation import form_presentation_error, present_configuration_payload
from .schedule_views import presentation_configuration
from .services import lock_controller
from .settings_forms import TokenBudgetForm
from .settings_models import TokenBudget


@never_cache
@require_GET
def index(request):
    document = presentation_configuration()
    budgets = []
    for budget in TokenBudget.objects.prefetch_related("runners"):
        metadata, error = present_configuration_payload(
            document,
            {
                "name": budget.name,
                "weekly": budget.weekly_remaining_percent,
                "five_hour": budget.five_hour_remaining_percent,
                "runners": [
                    {"pk": runner.pk, "name": str(runner)}
                    for runner in budget.runners.all()
                ],
            },
        )
        budgets.append({"pk": budget.pk, "metadata": metadata, "error": error})
    return render(request, "runner_control/token_budgets.html", {"budgets": budgets})


@never_cache
@require_http_methods(["GET", "POST"])
@transaction.atomic
def edit(request, budget_id=None):
    if request.method == "POST":
        lock_controller()
    saved = get_object_or_404(TokenBudget, pk=budget_id) if budget_id else TokenBudget()
    form = TokenBudgetForm(
        request.POST if request.method == "POST" else None, instance=saved
    )
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(
            request,
            "Token budget saved. Assigned runners check it before their next task or prompt.",
        )
        return redirect("token_budget_edit", budget_id=saved.pk)
    document = presentation_configuration()
    assigned, error = present_configuration_payload(
        document,
        (
            [{"pk": runner.pk, "name": str(runner)} for runner in saved.runners.all()]
            if saved.pk
            else []
        ),
    )
    return render(
        request,
        "runner_control/token_budget_edit.html",
        {
            "budget_id": saved.pk,
            "form": form,
            "form_unavailable": form_presentation_error(form, document),
            "assigned_runners": assigned or [],
            "assigned_error": error,
        },
    )
