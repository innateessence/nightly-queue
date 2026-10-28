from django.urls import path

from . import api, views

urlpatterns = [
    path("", views.Index.as_view(), name="index"),
    path("items/<int:pk>/remove/", views.remove_item, name="remove_item"),
    path("api/v1/items/", api.enqueue, name="api_enqueue"),
    path("api/v1/items/claim/", api.claim, name="api_claim"),
    path("api/v1/items/<int:pk>/finish/", api.finish, name="api_finish"),
]
