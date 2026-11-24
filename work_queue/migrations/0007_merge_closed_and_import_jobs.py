from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("work_queue", "0006_queueitem_closed"),
        ("work_queue", "0006_importjob_importjob_one_pending_import_per_kind"),
    ]

    operations = []
