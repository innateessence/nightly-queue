from django.urls import path

from . import plan_views, operations_views

urlpatterns = [
    path("activity/", plan_views.activity, name="runner_activity"),
    path("system/", plan_views.system, name="runner_system"),
    path(
        "runner/runs/<uuid:run_id>/retry/",
        operations_views.retry,
        name="runner_operator_retry",
    ),
    path(
        "runner/release/authorize/",
        operations_views.authorize_release,
        name="runner_release_authorize",
    ),
    path(
        "runner/release/<uuid:qualification_id>/revoke/",
        operations_views.revoke_release,
        name="runner_release_revoke",
    ),
    path(
        "runner/runs/<uuid:run_id>/cancel/",
        operations_views.cancel,
        name="runner_operator_cancel",
    ),
    path("runner/runs/", plan_views.history, name="runner_plan_history"),
    path("runner/runs/<uuid:run_id>/", plan_views.detail, name="runner_plan_detail"),
]
