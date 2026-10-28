from django.apps import AppConfig


class LegacyMigrationsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "hello"
    verbose_name = "Legacy migrations"
