from django.db import migrations, models


def merge_sources(apps, schema_editor):
    items = apps.get_model("work_queue", "QueueItem").objects.using(
        schema_editor.connection.alias
    )
    items.filter(source="auto").update(source="github")


def restore_sources(apps, schema_editor):
    items = apps.get_model("work_queue", "QueueItem").objects.using(
        schema_editor.connection.alias
    )
    items.filter(source="github").update(source="auto")


class Migration(migrations.Migration):
    dependencies = [
        (
            "work_queue",
            "0002_queueitem_issue_number_queueitem_issue_repository_and_more",
        ),
    ]

    operations = [
        migrations.RenameField(
            model_name="queueitem", old_name="queue", new_name="source"
        ),
        migrations.RunPython(merge_sources, restore_sources),
        migrations.AlterField(
            model_name="queueitem",
            name="source",
            field=models.CharField(
                choices=[("manual", "Manual"), ("github", "GitHub issue")],
                default="manual",
                max_length=6,
            ),
        ),
    ]
