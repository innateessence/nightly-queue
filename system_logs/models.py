from django.db import models
from django.utils import timezone


class SystemLog(models.Model):
    class Level(models.TextChoices):
        INFO = "info", "Info"
        WARNING = "warning", "Warning"
        ERROR = "error", "Error"
        CRITICAL = "critical", "Critical"

    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    level = models.CharField(max_length=8, choices=Level.choices)
    source = models.CharField(max_length=120)
    message = models.TextField()
    details = models.TextField(blank=True)

    class Meta:
        ordering = ["-created_at", "-pk"]
