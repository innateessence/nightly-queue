from django.core.management.color import no_style
from django.db import migrations, models
import django.db.models.deletion

import runner_control.validators


def seed_default(apps, schema_editor):
    Schedule = apps.get_model("runner_control", "RunnerSchedule")
    Schedule.objects.using(schema_editor.connection.alias).get_or_create(
        pk=1, defaults={"name": "Always on"}
    )


def copy_schedules(apps, schema_editor):
    Schedule = apps.get_model("runner_control", "RunnerSchedule")
    Settings = apps.get_model("runner_control", "RunnerSettings")
    database = schema_editor.connection.alias
    for runner in Settings.objects.using(database).all():
        if (
            runner.schedule_mode == "continuous"
            and runner.timezone == "UTC"
            and not runner.windows
        ):
            continue
        suffix = f" ({runner.pk})"
        schedule = Schedule.objects.using(database).create(
            pk=runner.pk + 1,
            name=f"{runner.name[:100 - len(suffix)]}{suffix}",
            schedule_mode=runner.schedule_mode,
            timezone=runner.timezone,
            windows=runner.windows,
        )
        Settings.objects.using(database).filter(pk=runner.pk).update(schedule=schedule)


def restore_schedules(apps, schema_editor):
    Settings = apps.get_model("runner_control", "RunnerSettings")
    database = schema_editor.connection.alias
    for runner in Settings.objects.using(database).select_related("schedule"):
        Settings.objects.using(database).filter(pk=runner.pk).update(
            schedule_mode=runner.schedule.schedule_mode,
            timezone=runner.schedule.timezone,
            windows=runner.schedule.windows,
        )


def reset_sequence(apps, schema_editor):
    Schedule = apps.get_model("runner_control", "RunnerSchedule")
    for statement in schema_editor.connection.ops.sequence_reset_sql(
        no_style(), [Schedule]
    ):
        schema_editor.execute(statement)


class Migration(migrations.Migration):
    dependencies = [
        ("runner_control", "0014_runnersettings_identity_runnersettings_name_and_more")
    ]

    operations = [
        migrations.CreateModel(
            name="RunnerSchedule",
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
                ("name", models.CharField(max_length=100, unique=True)),
                (
                    "schedule_mode",
                    models.CharField(
                        choices=[
                            ("continuous", "Continuous"),
                            ("windows", "Weekday windows"),
                        ],
                        default="continuous",
                        max_length=12,
                    ),
                ),
                (
                    "timezone",
                    models.CharField(
                        default="UTC",
                        max_length=100,
                        validators=[runner_control.validators.validate_timezone],
                    ),
                ),
                (
                    "windows",
                    models.JSONField(
                        blank=True,
                        default=list,
                        validators=[runner_control.validators.validate_windows],
                    ),
                ),
            ],
            options={"ordering": ["name", "pk"]},
        ),
        migrations.RunPython(seed_default, migrations.RunPython.noop),
        migrations.AddField(
            model_name="runnersettings",
            name="schedule",
            field=models.ForeignKey(
                default=1,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="runners",
                to="runner_control.runnerschedule",
            ),
        ),
        migrations.RunPython(copy_schedules, restore_schedules),
        migrations.RemoveField(model_name="runnersettings", name="schedule_mode"),
        migrations.RemoveField(model_name="runnersettings", name="timezone"),
        migrations.RemoveField(model_name="runnersettings", name="windows"),
        migrations.RunPython(reset_sequence, migrations.RunPython.noop),
    ]
