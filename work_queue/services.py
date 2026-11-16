import re
import uuid
from datetime import timedelta

from django.core.exceptions import ValidationError
from django.core.validators import URLValidator
from django.db import transaction
from django.db.models import Case, IntegerField, Q, When
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from credentials.models import APISettings
from projects.models import Project
from projects.validators import normalize_repository_url, validate_branch

from .models import MergeEvidence, QueueItem


def claim_auto_sync(saved):
    now = timezone.now()
    cutoff = now - timedelta(seconds=saved.auto_sync_interval_seconds)
    due = Q(last_auto_sync_at__isnull=True) | Q(last_auto_sync_at__lte=cutoff)
    matching = APISettings.objects.filter(pk=saved.pk).filter(due)
    return matching.update(last_auto_sync_at=now) == 1


def enqueue_item(
    *, project, title, instructions="", priority=QueueItem.Priority.NORMAL
):
    title = title.strip()
    instructions = instructions.strip()
    item = QueueItem(
        project=project, title=title, instructions=instructions, priority=priority
    )
    item.full_clean()
    item.save()
    return item


def issue_directives(body):
    dependencies = []
    branch = ""
    for line in body.splitlines():
        line = line.strip()
        if line.lower().startswith("depends on:"):
            value = line.split(":", 1)[1].strip()
            if dependencies or not re.fullmatch(
                r"#[1-9][0-9]*(?:\s*,\s*#[1-9][0-9]*)*", value
            ):
                return (
                    [],
                    branch,
                    "Malformed or cross-repository dependencies; use Depends on: #123, #124.",
                )
            dependencies = [int(part.strip()[1:]) for part in value.split(",")]
            if any(number >= 2**63 for number in dependencies):
                return [], branch, "Dependency issue number is too large."
        if line.lower().startswith("branch:"):
            value = line.split(":", 1)[1].strip()
            try:
                validate_branch(value)
                if not value or len(value) > 200 or value.startswith("refs/") or branch:
                    raise ValidationError("Invalid branch")
            except ValidationError:
                return dependencies, "", "Invalid or repeated Branch directive."
            branch = value
    return dependencies, branch, ""


@transaction.atomic
def import_issue(
    *, project, number, title, body, state, sync_lock_token=None, **metadata
):
    project = Project.objects.select_for_update().get(pk=project.pk)
    if sync_lock_token is not None and (
        project.sync_lock_token != sync_lock_token
        or not project.sync_lock_until
        or project.sync_lock_until <= timezone.now()
    ):
        raise ValidationError("Project synchronization lease expired.")
    repository = normalize_repository_url(project.repository_url)
    identity = {"project": project, "issue_number": number}
    repository_id = metadata.get("repository_id") or project.github_repository_id
    if repository_id:
        identity["github_repository_id"] = repository_id
        legacy = QueueItem.objects.filter(
            project=project,
            issue_number=number,
            github_repository_id__isnull=True,
            issue_repository=repository,
        )
        legacy.update(github_repository_id=repository_id)
    else:
        identity["issue_repository"] = repository
    item, created = QueueItem.objects.get_or_create(
        **identity,
        defaults={
            "source": QueueItem.Source.GITHUB,
            "title": title[:200],
            "instructions": body,
            "issue_repository": repository,
        },
    )
    upstream_updated = metadata.get("updated_at")
    upstream_updated = (
        parse_datetime(upstream_updated)
        if isinstance(upstream_updated, str)
        else upstream_updated
    )
    if (
        item.issue_updated_at
        and upstream_updated
        and upstream_updated < item.issue_updated_at
    ):
        return False
    dependencies, branch, error = issue_directives(body)
    labels = metadata.get("labels", [])
    values = {
        "issue_state": state,
        "issue_repository": repository,
        "source_metadata": {**metadata, "title": title, "body": body},
        "github_issue_id": metadata.get("issue_id"),
        "author_id": metadata.get("author_id"),
        "author_login": metadata.get("author_login", ""),
        "issue_labels": labels,
        "issue_created_at": metadata.get("created_at"),
        "issue_updated_at": metadata.get("updated_at"),
        "issue_revision": metadata.get("revision", ""),
        "issue_synced_at": timezone.now(),
        "dependencies": dependencies,
        "requested_branch": branch,
        "selection_error": error,
    }
    QueueItem.objects.filter(pk=item.pk).update(**values)
    priority = QueueItem.Priority.NORMAL
    if "vibearr:priority-high" in labels:
        priority = QueueItem.Priority.HIGH
    elif "vibearr:priority-low" in labels:
        priority = QueueItem.Priority.LOW
    QueueItem.objects.filter(pk=item.pk, status=QueueItem.Status.QUEUED).update(
        title=title[:200], instructions=body, priority=priority
    )
    if state == "closed" or {"vibearr:hold", "nq:hold"}.intersection(labels):
        from runner_control.models import Run

        Run.objects.filter(
            item=item, state=Run.State.ACTIVE, cancellation_requested_at__isnull=True
        ).update(
            cancellation_requested_at=timezone.now(),
            cancellation_reason="GitHub issue was closed or held.",
        )
    return created


def dependency_wait_reason(item):
    evidence = set(
        MergeEvidence.objects.filter(
            repository_id=item.github_repository_id
        ).values_list("issue_number", flat=True)
    )
    issues = {
        issue.issue_number: issue
        for issue in QueueItem.objects.filter(
            project=item.project, github_repository_id=item.github_repository_id
        )
    }
    visited = set()
    pending = [(item.issue_number, ())]
    while pending:
        number, ancestors = pending.pop()
        if number in ancestors:
            return "Cyclic issue dependencies."
        if number in visited:
            continue
        visited.add(number)
        issue = issues.get(number)
        if issue is None:
            return f"Missing dependency #{number}."
        if issue.selection_error:
            return issue.selection_error
        for dependency in issue.dependencies:
            if dependency not in issues and dependency not in evidence:
                return f"Missing dependency #{dependency}."
            if dependency not in evidence:
                pending.append((dependency, (*ancestors, number)))
    unmet = [number for number in item.dependencies if number not in evidence]
    return f"Waiting for verified merge of #{unmet[0]}." if unmet else ""


def source_wait_reason(item):
    if item.source == QueueItem.Source.MANUAL:
        return ""
    project = item.project
    if not project.autonomous:
        return "Project autonomous execution is disabled."
    if item.issue_state != QueueItem.IssueState.OPEN:
        return "Issue is closed."
    if {"vibearr:hold", "nq:hold"}.intersection(item.issue_labels):
        return "Issue is held on GitHub."
    complete = (
        item.github_repository_id
        and item.github_issue_id
        and item.author_id
        and item.issue_created_at
        and item.issue_updated_at
        and item.issue_revision
        and project.github_owner_id
        and project.enabled_at
        and (project.base_branch or project.github_default_branch)
    )
    if not complete or item.github_repository_id != project.github_repository_id:
        return "Waiting for complete GitHub source context."
    if project.sync_error:
        return "Waiting for successful GitHub reconciliation."
    permitted = project.permitted_author_ids or [project.github_owner_id]
    if item.author_id not in permitted:
        return "Issue author is not permitted."
    if not project.backfill_issues and item.issue_created_at < project.enabled_at:
        return "Historical issue; project backfill is disabled."
    if (
        item.result_url
        or MergeEvidence.objects.filter(
            repository_id=item.github_repository_id, issue_number=item.issue_number
        ).exists()
    ):
        return "Issue already has delivery or merge evidence."
    if item.status == QueueItem.Status.QUEUED:
        from runner_control.models import RunEvidence

        if RunEvidence.objects.filter(
            run__item=item, kind="verified_delivery"
        ).exists():
            return "Prior delivery requires reconciliation before another execution."
    if item.selection_error:
        return item.selection_error
    branch = (
        item.branch_name
        or item.requested_branch
        or f"vibearr/issue-{item.issue_number}"
    )
    if (
        branch == (project.base_branch or project.github_default_branch)
        or branch == "HEAD"
    ):
        return "Requested branch is the repository base branch."
    collisions = QueueItem.objects.filter(project=project).exclude(pk=item.pk)
    if collisions.filter(Q(branch_name=branch) | Q(requested_branch=branch)).exists():
        return "Requested branch collides with another work item."
    return dependency_wait_reason(item)


def claimable_items():
    queued = QueueItem.objects.filter(status=QueueItem.Status.QUEUED).select_related(
        "project"
    )
    eligible = [item.pk for item in queued if not source_wait_reason(item)]
    order = Case(
        When(priority="high", then=0),
        When(priority="normal", then=1),
        default=2,
        output_field=IntegerField(),
    )
    return QueueItem.objects.filter(pk__in=eligible).order_by(order, "created_at", "pk")


def claim_next(*, execution_ref=""):
    while True:
        item = claimable_items().select_related("project").first()
        if item is None:
            return None
        claimed = claim_item(item, execution_ref=execution_ref)
        if claimed is not None:
            return claimed


@transaction.atomic
def claim_item(item, *, execution_ref=""):
    project_id = item.project_id
    Project.objects.select_for_update().get(pk=item.project_id)
    item = (
        QueueItem.objects.select_for_update().select_related("project").get(pk=item.pk)
    )
    if item.project_id != project_id:
        return None
    if item.status != QueueItem.Status.QUEUED or source_wait_reason(item):
        return None
    repository_url = item.repository_url or normalize_repository_url(
        item.project.repository_url
    )
    base_branch = (
        item.base_branch
        if item.repository_url
        else (item.project.base_branch or item.project.github_default_branch)
    )
    if item.source == QueueItem.Source.GITHUB:
        repository_url = item.repository_url or item.issue_repository
    branch = item.branch_name or item.requested_branch
    if not branch:
        branch = (
            f"vibearr/issue-{item.issue_number}"
            if item.issue_number
            else f"vibearr/manual-{item.pk}"
        )
    snapshot = item.source_snapshot or {
        "title": item.title,
        "body": item.instructions,
        "repository_id": item.github_repository_id,
        "issue_number": item.issue_number,
        "revision": item.issue_revision,
        "metadata": item.source_metadata,
    }
    token = uuid.uuid4()
    QueueItem.objects.filter(pk=item.pk, status=QueueItem.Status.QUEUED).update(
        status=QueueItem.Status.RUNNING,
        claim_token=token,
        started_at=timezone.now(),
        finished_at=None,
        branch_name=branch,
        execution_ref=execution_ref,
        repository_url=repository_url,
        base_branch=base_branch,
        source_snapshot=snapshot,
        result_url="",
        error_summary="",
    )
    return QueueItem.objects.select_related("project").get(pk=item.pk)


@transaction.atomic
def record_verified_merge(
    *,
    repository_id,
    issue_number,
    pull_number,
    merge_sha,
    merged_at,
    provenance,
    operation_id,
):
    """Trusted provider reconciliation only; never call with task-authored evidence."""
    if not all(
        type(value) is int and 0 < value < 2**63
        for value in (repository_id, issue_number, pull_number)
    ):
        raise ValidationError("Merge evidence requires positive provider IDs.")
    if (
        not re.fullmatch(r"[0-9a-f]{40}(?:[0-9a-f]{24})?", merge_sha)
        or not provenance
        or not merged_at
    ):
        raise ValidationError(
            "Merge evidence requires a provider merge commit, timestamp and provenance."
        )
    projects = Project.objects.filter(github_repository_id=repository_id).order_by("pk")
    list(projects.select_for_update())
    values = dict(
        repository_id=repository_id,
        issue_number=issue_number,
        pull_number=pull_number,
        merge_sha=merge_sha,
        merged_at=merged_at,
        provenance=provenance,
    )
    previous = MergeEvidence.objects.filter(
        Q(operation_id=operation_id)
        | Q(repository_id=repository_id, issue_number=issue_number)
    ).first()
    if previous:
        if any(getattr(previous, key) != value for key, value in values.items()):
            raise ValidationError(
                "Merge evidence conflicts with previously verified evidence."
            )
        return previous
    evidence = MergeEvidence(operation_id=operation_id, **values)
    evidence.full_clean()
    evidence.save()
    return evidence


def finish_item(
    *, item_id, claim_token, status, result_url="", error_summary="", runner_run=None
):
    from runner_control.models import Run

    if status not in (QueueItem.Status.SUCCEEDED, QueueItem.Status.FAILED):
        raise ValidationError({"status": "Choose succeeded or failed."})
    result_field = QueueItem._meta.get_field("result_url")
    try:
        result_field.clean(result_url, None)
        if result_url:
            URLValidator(schemes=["http", "https"])(result_url)
    except ValidationError as exc:
        raise ValidationError({"result_url": exc.messages})
    running = QueueItem.objects.filter(pk=item_id, status=QueueItem.Status.RUNNING)
    claimed = running.filter(claim_token=claim_token)
    owned = Run.objects.filter(item_id=item_id, state=Run.State.ACTIVE)
    if runner_run is None and owned.exists():
        return 0
    if runner_run is not None and not owned.filter(pk=runner_run.pk).exists():
        return 0
    return claimed.update(
        status=status,
        finished_at=timezone.now(),
        result_url=result_url,
        error_summary=error_summary,
    )


@transaction.atomic
def requeue_items(items):
    """Stop the previous workflow before requeuing."""
    from runner_control.models import Run
    from runner_control.services import lock_controller

    lock_controller()
    statuses = [QueueItem.Status.RUNNING, QueueItem.Status.FAILED]
    retryable = items.filter(status__in=statuses)
    project_ids = retryable.values_list("project_id", flat=True)
    list(Project.objects.select_for_update().filter(pk__in=project_ids).order_by("pk"))
    list(retryable.select_for_update().order_by("pk"))
    item_ids = list(retryable.values_list("pk", flat=True))
    previous = Run.objects.filter(item_id__in=item_ids).exclude(
        state=Run.State.OBSOLETE
    )
    previous.filter(finished_at__isnull=True).update(finished_at=timezone.now())
    previous.update(
        state=Run.State.OBSOLETE, active_slot=None, updated_at=timezone.now()
    )
    for item in retryable.select_for_update():
        if item.source == QueueItem.Source.GITHUB and item.source_metadata:
            item.title = item.source_metadata.get("title", item.title)[:200]
            item.instructions = item.source_metadata.get("body", item.instructions)
            item.source_snapshot = {}
            item.save(update_fields=["title", "instructions", "source_snapshot"])
    return retryable.update(
        status=QueueItem.Status.QUEUED,
        claim_token=None,
        started_at=None,
        finished_at=None,
        execution_ref="",
        result_url="",
        error_summary="",
    )


def refresh_issue_for_launch(*, item_id, claim_token, token):
    """Fresh provider authorization for feature 04; transport errors always block launch."""
    from .github import GitHubAPIError, issue_comments, read_issue

    item = QueueItem.objects.select_related("project").get(pk=item_id)
    if item.status != QueueItem.Status.RUNNING or str(item.claim_token) != str(
        claim_token
    ):
        raise ValidationError("Item is not running under this claim.")
    if item.source == QueueItem.Source.MANUAL:
        return item.source_snapshot
    comments = None
    if "comments" not in item.source_snapshot:
        comments = issue_comments(item.project, item.issue_number, token)
    fresh = read_issue(item.project, item.issue_number, token)
    if (
        fresh["number"] != item.issue_number
        or fresh["issue_id"] != item.github_issue_id
    ):
        raise GitHubAPIError("Issue identity changed; launch is blocked.")
    import_issue(project=item.project, repository_id=item.github_repository_id, **fresh)
    with transaction.atomic():
        Project.objects.select_for_update().get(pk=item.project_id)
        item = (
            QueueItem.objects.select_for_update()
            .select_related("project")
            .get(pk=item_id)
        )
        if item.status != QueueItem.Status.RUNNING or str(item.claim_token) != str(
            claim_token
        ):
            raise ValidationError("Item is not running under this claim.")
        reason = source_wait_reason(item)
        if reason:
            raise ValidationError(reason)
        if fresh["revision"] != item.source_snapshot.get("revision"):
            raise ValidationError(
                "Issue instructions changed after claim; retry explicitly with refreshed context."
            )
        if comments is not None and "comments" not in item.source_snapshot:
            item.source_snapshot["comments"] = comments
            item.save(update_fields=["source_snapshot"])
        return item.source_snapshot
