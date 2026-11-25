from django.urls import path

from . import (
    plan_views,
    operations_views,
    configuration_views,
    schedule_views,
    deletion_views,
)

urlpatterns = [
    path("schedules/", schedule_views.index, name="runner_schedule_list"),
    path("schedules/add/", schedule_views.edit, name="runner_schedule_add"),
    path(
        "schedules/<int:schedule_id>/", schedule_views.edit, name="runner_schedule_edit"
    ),
    path(
        "schedules/<int:schedule_id>/delete/",
        deletion_views.schedule,
        name="runner_schedule_delete",
    ),
    path("coding-runners/", configuration_views.index, name="coding_runner_list"),
    path("coding-runners/add/", configuration_views.edit, name="coding_runner_add"),
    path(
        "coding-runners/<int:settings_id>/",
        configuration_views.edit,
        name="coding_runner_edit",
    ),
    path(
        "coding-runners/<int:settings_id>/delete/",
        deletion_views.runner,
        name="coding_runner_delete",
    ),
    path("activity/", plan_views.activity, name="runner_activity"),
    path("system/", plan_views.system, name="runner_system"),
    path(
        "runner/runs/<uuid:run_id>/retry/",
        operations_views.retry,
        name="runner_operator_retry",
    ),
    path(
        "runner/runs/<uuid:run_id>/cancel/",
        operations_views.cancel,
        name="runner_operator_cancel",
    ),
    path("runner/runs/", plan_views.history, name="runner_plan_history"),
    path("runner/runs/<uuid:run_id>/", plan_views.detail, name="runner_plan_detail"),
]
