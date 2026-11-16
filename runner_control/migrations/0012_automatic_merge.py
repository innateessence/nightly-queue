import django.db.models.deletion
from django.db import migrations, models

import runner_control.validators


class Migration(migrations.Migration):
    dependencies = [("runner_control", "0011_native_run_protocol")]

    operations = [
        migrations.AddField(
            model_name="runnersettings",
            name="merge_profile",
            field=models.JSONField(
                blank=True,
                default=dict,
                validators=[runner_control.validators.validate_merge_profile],
            ),
        ),
        migrations.CreateModel(
            name="RepositoryMergeFence",
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
                ("repository_id", models.PositiveBigIntegerField(unique=True)),
                ("publication_id", models.UUIDField()),
                ("intent_id", models.UUIDField()),
                ("head_sha", models.CharField(max_length=64)),
                ("base_sha", models.CharField(max_length=64)),
                ("state", models.CharField(default="pending", max_length=20)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "run",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="merge_fences",
                        to="runner_control.run",
                    ),
                ),
            ],
        ),
    ]
