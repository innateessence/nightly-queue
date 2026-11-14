import logging

from celery import shared_task
from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.views.decorators.debug import sensitive_variables

from credentials.models import APISettings, Credential
from projects.models import Project

from .github import GitHubAPIError, repository_issues
from .services import import_issue

logger = logging.getLogger(__name__)


@shared_task
@sensitive_variables()
def import_auto_issues():
    saved = APISettings.objects.select_related("github_credential").filter(pk=1).first()
    credential = saved.github_credential if saved else None
    if credential is None:
        logger.info(
            "Auto issue import skipped: select a GitHub API credential in Settings."
        )
        return {"projects": 0, "created": 0, "failed": 0, "skipped": True}
    if (
        credential.provider != Credential.Provider.GITHUB
        or credential.kind != Credential.Kind.TOKEN
    ):
        logger.error(
            "Auto issue import skipped: the selected credential must be a GitHub token."
        )
        return {"projects": 0, "created": 0, "failed": 0, "skipped": True}
    try:
        token = credential.get_secret()["secret"]
    except (ImproperlyConfigured, ValidationError):
        logger.error(
            "Auto issue import skipped: unable to decrypt the selected credential."
        )
        return {"projects": 0, "created": 0, "failed": 0, "skipped": True}
    result = {"projects": 0, "created": 0, "failed": 0, "skipped": False}
    for project in Project.objects.filter(autonomous=True).iterator():
        try:
            for issue in repository_issues(project.repository_url, token):
                result["created"] += int(import_issue(project=project, **issue))
        except (GitHubAPIError, ValidationError):
            logger.warning(
                "Auto issue import failed for project %s; check API access and retry next hour.",
                project.pk,
            )
            result["failed"] += 1
            continue
        result["projects"] += 1
    return result


@shared_task
def add(x, y):
    return x + y


@shared_task
def nightly_greeting():
    from datetime import datetime, timezone

    stamp = datetime.now(timezone.utc).isoformat()
    logger.info("nightly_greeting fired at %s", stamp)
    return {"greeting": "good night", "at": stamp}
