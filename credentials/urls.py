from django.urls import path

from . import views

urlpatterns = [
    path("credentials/", views.credential_list, name="credential_list"),
]
