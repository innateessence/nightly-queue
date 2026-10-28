from django.apps import AppConfig


class WorkQueueConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "work_queue"
    verbose_name = "Work queue"
