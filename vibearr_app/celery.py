import os

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "vibearr_app.settings")

from celery import Celery

app = Celery("vibearr_app")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()
