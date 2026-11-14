from django.urls import path
from django.views.generic import RedirectView

from . import api, views

urlpatterns = [
    path("", views.Index.as_view(), name="index"),
    path(
        "auto/",
        RedirectView.as_view(pattern_name="index", query_string=True),
        name="auto_queue",
    ),
    path("items/<int:pk>/remove/", views.remove_item, name="remove_item"),
    path("issues/sync/", views.sync_issues, name="sync_issues"),
    path("api/v1/items/", api.enqueue, name="api_enqueue"),
    path("api/v1/items/claim/", api.claim, name="api_claim"),
    path("api/v1/items/<int:pk>/finish/", api.finish, name="api_finish"),
]
