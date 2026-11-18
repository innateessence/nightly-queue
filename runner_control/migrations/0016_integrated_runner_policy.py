from django.db import migrations


HOSTS = [
    "chatgpt.com",
    "auth.openai.com",
    "api.anthropic.com",
    "claude.ai",
    "platform.claude.com",
]


def converted(policy):
    if (
        not isinstance(policy, dict)
        or policy.get("runtime_profile") != "native-default"
    ):
        return policy
    policy = dict(policy)
    policy["network_hosts"] = list(
        dict.fromkeys([*policy.get("network_hosts", []), *HOSTS])
    )
    if policy.get("validation_profile") == "project-default":
        policy["validation_profile"] = "vibearr-python312-v1"
    return policy


def migrate_policy(apps, schema_editor):
    database = schema_editor.connection.alias
    settings = apps.get_model("runner_control", "RunnerSettings")
    for row in settings.objects.using(database).all():
        row.execution_policy = converted(row.execution_policy)
        row.enabled = False
        row.save(using=database, update_fields=["execution_policy", "enabled"])
    overrides = apps.get_model("runner_control", "ProjectRunnerSettings")
    for row in overrides.objects.using(database).all():
        policy = row.overrides.get("execution_policy", {})
        if policy.get("mode") == "replace":
            policy["value"] = converted(policy.get("value"))
            row.save(using=database, update_fields=["overrides"])


class Migration(migrations.Migration):
    dependencies = [("runner_control", "0015_reusable_runner_schedules")]
    operations = [migrations.RunPython(migrate_policy, migrations.RunPython.noop)]
