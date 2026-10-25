from django.urls import path

from . import views

urlpatterns = [
    path("", views.Index.as_view(), name="index"),
    path("items/<int:pk>/remove/", views.remove_item, name="remove_item"),
]
