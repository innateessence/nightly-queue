import os

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "nightly_queue.settings")

import django
from celery import Celery

django.setup()

app = Celery("nightly_queue")
app.config_from_object("django.conf:settings")
app.autodiscover_tasks()
