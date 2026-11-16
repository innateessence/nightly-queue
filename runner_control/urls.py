from django.urls import path

from . import (
    api,
    approval_api,
    configuration_api,
    gate_api,
    plan_api,
    secrets,
    operations_api,
    native_api,
)

from . import recovery_api, source_api

urlpatterns = [
    path("candidates/", native_api.native_candidates, name="runner_candidates"),
    path(
        "runs/<uuid:run_id>/native/status/",
        native_api.native_status,
        name="runner_native_status",
    ),
    path(
        "runs/<uuid:run_id>/source/refresh/",
        source_api.refresh_source,
        name="runner_refresh_source",
    ),
    path("health/", operations_api.health, name="runner_health"),
    path(
        "release/<uuid:qualification_id>/",
        operations_api.release_suite,
        name="runner_release_suite",
    ),
    path(
        "release/<uuid:qualification_id>/claim/",
        operations_api.release_claim,
        name="runner_release_claim",
    ),
    path("retention/", operations_api.retention, name="runner_retention"),
    path(
        "runs/<uuid:run_id>/retention/ack/",
        operations_api.retention_ack,
        name="runner_retention_ack",
    ),
    path(
        "runs/<uuid:run_id>/retention/workspace/",
        operations_api.retention_workspace,
        name="runner_retention_workspace",
    ),
    path(
        "runs/<uuid:run_id>/operations/<uuid:operation_id>/",
        recovery_api.operation_detail,
        name="runner_operation",
    ),
    path("controller/", recovery_api.bind_controller, name="runner_controller"),
    path(
        "runs/<uuid:run_id>/cancellation/",
        recovery_api.cancellation,
        name="runner_cancellation",
    ),
    path(
        "runs/<uuid:run_id>/contexts/<uuid:document_id>/",
        plan_api.context_detail,
        name="runner_context_detail",
    ),
    path(
        "runs/<uuid:run_id>/plans/<uuid:document_id>/",
        plan_api.plan_detail,
        name="runner_plan_document",
    ),
    path("runs/<uuid:run_id>/contexts/", plan_api.contexts, name="runner_contexts"),
    path("runs/<uuid:run_id>/plans/", plan_api.plans, name="runner_plans"),
    path(
        "runs/<uuid:run_id>/handoff/<uuid:operation_id>/",
        plan_api.handoff_detail,
        name="runner_handoff_detail",
    ),
    path("runs/<uuid:run_id>/handoff/", plan_api.handoff, name="runner_handoff"),
    path("secrets/", secrets.selected_secret, name="runner_selected_secret"),
    path("gates/", gate_api.gates, name="runner_gates"),
    path("gates/report/", gate_api.report, name="runner_gate_report"),
    path(
        "configuration/", configuration_api.configuration, name="runner_configuration"
    ),
    path(
        "runs/<uuid:run_id>/approvals/",
        approval_api.collection,
        name="runner_approvals",
    ),
    path(
        "runs/<uuid:run_id>/approvals/<uuid:approval_id>/",
        approval_api.detail,
        name="runner_approval",
    ),
    path(
        "runs/<uuid:run_id>/approvals/<uuid:approval_id>/consume/",
        approval_api.consume,
        name="runner_approval_consume",
    ),
    path("identity/", api.identity, name="runner_identity"),
    path("runs/claim/", api.claim, name="runner_claim"),
    path("runs/active/", api.active, name="runner_active"),
    path("runs/<uuid:run_id>/", api.detail, name="runner_detail"),
    path("runs/<uuid:run_id>/claim/", api.recover, name="runner_recover"),
    path("runs/<uuid:run_id>/progress/", api.progress, name="runner_progress"),
    path("runs/<uuid:run_id>/artifacts/", api.artifact, name="runner_artifact"),
    path("runs/<uuid:run_id>/evidence/", api.evidence, name="runner_evidence"),
    path("runs/<uuid:run_id>/finish/", api.finish, name="runner_finish"),
]
