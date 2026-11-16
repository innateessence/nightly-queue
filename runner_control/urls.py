from django.urls import path

from . import api, configuration_api

urlpatterns = [
    path(
        "configuration/", configuration_api.configuration, name="runner_configuration"
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
