from django.urls import path

from .views import system_logs

urlpatterns = [path("logs/", system_logs, name="system_logs")]
