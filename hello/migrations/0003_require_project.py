from django.db import migrations, models
import django.db.models.deletion


def require_assigned_projects(apps, schema_editor):
    queue_item = apps.get_model("hello", "QueueItem")
    items = queue_item.objects.using(schema_editor.connection.alias)
    if not items.filter(project__isnull=True).exists():
        return
    message = (
        "Existing queue items need projects. Run 'python manage.py migrate hello 0002', "
        "create projects, then run 'python manage.py backfill_projects --project ID "
        "--items ITEM_ID ...' (or --all). Finally run 'python manage.py migrate' again."
    )
    raise RuntimeError(message)


class Migration(migrations.Migration):
    dependencies = [("hello", "0002_projects")]

    operations = [
        migrations.RunPython(require_assigned_projects, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="queueitem",
            name="project",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="items",
                to="hello.project",
            ),
        ),
    ]
