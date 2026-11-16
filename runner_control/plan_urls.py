from django.urls import path

from . import plan_views

urlpatterns = [
    path("runner/runs/", plan_views.history, name="runner_plan_history"),
    path("runner/runs/<uuid:run_id>/", plan_views.detail, name="runner_plan_detail"),
]
