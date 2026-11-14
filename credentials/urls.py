from django.urls import path

from . import views

urlpatterns = [
    path("settings/", views.api_settings, name="api_settings"),
    path("credentials/", views.credential_list, name="credential_list"),
]
