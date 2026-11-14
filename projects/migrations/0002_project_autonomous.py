from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("projects", "0001_initial")]

    operations = [
        migrations.AddField(
            model_name="project",
            name="autonomous",
            field=models.BooleanField(default=False),
        ),
    ]
