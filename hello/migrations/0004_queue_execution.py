from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("hello", "0003_require_project")]

    operations = [
        migrations.AddField(
            model_name="queueitem",
            name="base_branch",
            field=models.CharField(blank=True, editable=False, max_length=200),
        ),
        migrations.AddField(
            model_name="queueitem",
            name="branch_name",
            field=models.CharField(blank=True, max_length=200),
        ),
        migrations.AddField(
            model_name="queueitem",
            name="claim_token",
            field=models.UUIDField(blank=True, editable=False, null=True),
        ),
        migrations.AddField(
            model_name="queueitem",
            name="error_summary",
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name="queueitem",
            name="execution_ref",
            field=models.CharField(blank=True, max_length=200),
        ),
        migrations.AddField(
            model_name="queueitem",
            name="finished_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="queueitem",
            name="repository_url",
            field=models.URLField(blank=True, editable=False, max_length=500),
        ),
        migrations.AddField(
            model_name="queueitem",
            name="result_url",
            field=models.URLField(blank=True, max_length=500),
        ),
        migrations.AddField(
            model_name="queueitem",
            name="started_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="queueitem",
            name="status",
            field=models.CharField(
                choices=[
                    ("queued", "Queued"),
                    ("running", "Running"),
                    ("succeeded", "Succeeded"),
                    ("failed", "Failed"),
                ],
                default="queued",
                max_length=9,
            ),
        ),
        migrations.AddIndex(
            model_name="queueitem",
            index=models.Index(
                fields=["status", "created_at", "id"], name="queue_claim_order"
            ),
        ),
    ]
