from django.urls import path

from . import views
from runner_control import deletion_views

urlpatterns = [
    path(
        "credentials/<int:credential_id>/delete/",
        deletion_views.credential,
        name="credential_delete",
    ),
    path(
        "credentials/<int:credential_id>/edit/",
        views.credential_edit,
        name="credential_edit",
    ),
    path(
        "credentials/<int:credential_id>/revoke/",
        views.credential_revoke,
        name="credential_revoke",
    ),
    path("settings/", views.api_settings, name="api_settings"),
    path("credentials/", views.credential_list, name="credential_list"),
]
