from django.urls import path

from . import views

urlpatterns = [
    path("", views.index, name="index"),
    path("items/<int:pk>/remove/", views.remove_item, name="remove_item"),
]
