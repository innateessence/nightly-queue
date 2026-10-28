from django.core.exceptions import ValidationError
from django.db import models

from .validators import normalize_repository_url, validate_branch


class Project(models.Model):
    name = models.CharField(max_length=200)
    repository_url = models.URLField(max_length=500)
    base_branch = models.CharField(
        max_length=200, blank=True, validators=[validate_branch]
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name", "pk"]

    def clean(self):
        super().clean()
        if not self.name or not self.name.strip():
            raise ValidationError({"name": "Enter a project name."})
        self.name = self.name.strip()
        self.repository_url = normalize_repository_url(self.repository_url)

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.name} (#{self.pk})"


class QueueItem(models.Model):
    class Priority(models.TextChoices):
        LOW = "low", "Low"
        NORMAL = "normal", "Normal"
        HIGH = "high", "High"

    project = models.ForeignKey(Project, on_delete=models.PROTECT, related_name="items")
    title = models.CharField(max_length=200)
    instructions = models.TextField(blank=True)
    priority = models.CharField(
        max_length=6, choices=Priority.choices, default=Priority.NORMAL
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "pk"]

    def __str__(self):
        return self.title
