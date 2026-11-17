from django.db import migrations, models
from django.utils import timezone


def close_queued_issues(apps, schema_editor):
    items = apps.get_model("work_queue", "QueueItem").objects.using(
        schema_editor.connection.alias
    )
    items.filter(source="github", issue_state="closed", status="queued").update(
        status="closed", finished_at=timezone.now()
    )


def restore_queued_issues(apps, schema_editor):
    items = apps.get_model("work_queue", "QueueItem").objects.using(
        schema_editor.connection.alias
    )
    items.filter(source="github", status="closed").update(
        status="queued", finished_at=None
    )


class Migration(migrations.Migration):
    dependencies = [
        (
            "work_queue",
            "0005_alter_queueitem_options_alter_queueitem_managers_and_more",
        )
    ]

    operations = [
        migrations.AlterField(
            model_name="queueitem",
            name="status",
            field=models.CharField(
                choices=[
                    ("queued", "Queued"),
                    ("running", "Running"),
                    ("succeeded", "Succeeded"),
                    ("failed", "Failed"),
                    ("closed", "Closed"),
                ],
                default="queued",
                max_length=9,
            ),
        ),
        migrations.RunPython(close_queued_issues, restore_queued_issues),
    ]
