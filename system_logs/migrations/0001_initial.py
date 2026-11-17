import django.utils.timezone
from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True
    dependencies = []

    operations = [
        migrations.CreateModel(
            name="SystemLog",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "created_at",
                    models.DateTimeField(
                        db_index=True, default=django.utils.timezone.now
                    ),
                ),
                (
                    "level",
                    models.CharField(
                        choices=[
                            ("info", "Info"),
                            ("warning", "Warning"),
                            ("error", "Error"),
                            ("critical", "Critical"),
                        ],
                        max_length=8,
                    ),
                ),
                ("source", models.CharField(max_length=120)),
                ("message", models.TextField()),
                ("details", models.TextField(blank=True)),
            ],
            options={"ordering": ["-created_at", "-pk"]},
        ),
    ]
