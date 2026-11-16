import logging
import uuid
from datetime import timedelta

from django.db.models import Q
from django.db import transaction
from django.utils import timezone

from celery import shared_task
from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.views.decorators.debug import sensitive_variables

from credentials.models import APISettings, Credential
from projects.models import Project

from .github import GitHubAPIError, repository_issues, repository_metadata
from .models import QueueItem
from .services import claim_auto_sync, import_issue

logger = logging.getLogger(__name__)


@shared_task
@sensitive_variables()
def import_auto_issues(scheduled=False):
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
    if scheduled and not claim_auto_sync(saved):
        return {"projects": 0, "created": 0, "failed": 0, "skipped": True}
    result = {"projects": 0, "created": 0, "failed": 0, "skipped": False}
    for project in Project.objects.filter(autonomous=True).iterator():
        now = timezone.now()
        available = Q(sync_lock_until__isnull=True) | Q(sync_lock_until__lte=now)
        ready = Q(sync_not_before__isnull=True) | Q(sync_not_before__lte=now)
        lock = uuid.uuid4()
        matching = Project.objects.filter(pk=project.pk).filter(available, ready)
        if not matching.update(
            sync_lock_token=lock,
            sync_lock_until=now + timedelta(minutes=10),
            sync_attempt_at=now,
        ):
            continue
        owned = Project.objects.filter(pk=project.pk, sync_lock_token=lock)
        try:
            repository = repository_metadata(
                project.repository_url, token, project.github_repository_id
            )
            if (
                project.github_repository_id
                and project.github_repository_id != repository["repository_id"]
            ):
                raise GitHubAPIError("Repository identity changed.")
            with transaction.atomic():
                current = Project.objects.select_for_update().get(pk=project.pk)
                if (
                    current.sync_lock_token != lock
                    or not current.sync_lock_until
                    or current.sync_lock_until <= timezone.now()
                ):
                    raise GitHubAPIError("Project synchronization lease expired.")
                QueueItem.objects.filter(
                    project=project,
                    source="github",
                    github_repository_id__isnull=True,
                    issue_repository=project.repository_url,
                ).update(
                    github_repository_id=repository["repository_id"],
                    issue_repository=repository["repository_url"],
                )
                owned.update(
                    github_repository_id=repository["repository_id"],
                    github_owner_id=repository["owner_id"],
                    github_default_branch=repository["default_branch"],
                    repository_url=repository["repository_url"],
                )
            project.refresh_from_db()
            etags = project.sync_etags.copy()
            for issue in repository_issues(project.repository_url, token, etags=etags):
                if not owned.filter(sync_lock_until__gt=timezone.now()).update(
                    sync_lock_until=timezone.now() + timedelta(minutes=10)
                ):
                    raise GitHubAPIError("Project synchronization lease expired.")
                result["created"] += int(
                    import_issue(
                        project=project,
                        sync_lock_token=lock,
                        repository_id=repository["repository_id"],
                        **issue,
                    )
                )
            owned.update(
                sync_success_at=timezone.now(),
                sync_etags=etags,
                sync_error="",
                sync_not_before=None,
            )
        except (GitHubAPIError, ValidationError) as exc:
            logger.warning(
                "Auto issue import failed for project %s; check API access and retry next scheduled sync.",
                project.pk,
            )
            retry = getattr(exc, "retry_at", None) or timezone.now() + timedelta(
                seconds=60
            )
            owned.update(
                sync_error="GitHub synchronization failed; source execution is waiting.",
                sync_not_before=retry,
            )
            result["failed"] += 1
            continue
        finally:
            owned.update(sync_lock_token=None, sync_lock_until=None)
        result["projects"] += 1
    if result["projects"] and not result["failed"]:
        APISettings.objects.filter(pk=saved.pk).update(
            last_auto_sync_success_at=timezone.now()
        )
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
