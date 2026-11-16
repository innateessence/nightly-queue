from django.core.exceptions import ValidationError


def locked_configuration_snapshot(project, expected_revision):
    # Feature 02 replaces this hook with locked, revision-checked effective settings.
    if expected_revision:
        raise ValidationError(
            {"configuration_revision": "Configuration revisions are not available yet."}
        )
    return "", {}
