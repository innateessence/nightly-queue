from django.db import migrations


def move_content_types(apps, schema_editor, *, reverse=False):
    content_type = apps.get_model("contenttypes", "ContentType")
    types = content_type.objects.using(schema_editor.connection.alias)
    destinations = [("project", "projects"), ("queueitem", "work_queue")]
    for model, destination in destinations:
        source = "hello"
        if reverse:
            source, destination = destination, source
        types.filter(app_label=source, model=model).update(app_label=destination)


def forwards(apps, schema_editor):
    move_content_types(apps, schema_editor)


def backwards(apps, schema_editor):
    move_content_types(apps, schema_editor, reverse=True)


class Migration(migrations.Migration):
    dependencies = [
        ("hello", "0004_queue_execution"),
        ("contenttypes", "0002_remove_content_type_name"),
    ]

    operations = [
        # Keep table rows and content type IDs, including permission assignments.
        migrations.RunPython(forwards, backwards),
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.DeleteModel(name="QueueItem"),
                migrations.DeleteModel(name="Project"),
            ],
        ),
    ]
