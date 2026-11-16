from django.urls import path

from . import approval_views

urlpatterns = [
    path("runner/approvals/", approval_views.inbox, name="runner_approval_inbox"),
    path(
        "runner/approvals/<uuid:approval_id>/decision/",
        approval_views.decide,
        name="runner_approval_decide",
    ),
]
