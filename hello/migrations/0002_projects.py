from django.db import migrations, models
import django.db.models.deletion
import hello.validators


class Migration(migrations.Migration):
    dependencies = [("hello", "0001_initial")]

    operations = [
        migrations.CreateModel(
            name="Project",
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
                ("name", models.CharField(max_length=200)),
                ("repository_url", models.URLField(max_length=500)),
                (
                    "base_branch",
                    models.CharField(
                        blank=True,
                        max_length=200,
                        validators=[hello.validators.validate_branch],
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "ordering": ["name", "pk"],
            },
        ),
        migrations.AddField(
            model_name="queueitem",
            name="project",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="items",
                to="hello.project",
            ),
        ),
    ]
