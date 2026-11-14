import os

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "nightly_queue.settings")

from celery import Celery

app = Celery("nightly_queue")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()
