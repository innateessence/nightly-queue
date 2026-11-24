"""Current read authorization observes human merges after delivery releases its claim."""

from datetime import datetime, timedelta, timezone as datetime_timezone
from urllib.parse import quote
from uuid import NAMESPACE_URL, uuid4, uuid5

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.views.decorators.debug import sensitive_variables

from credentials.models import Credential
from projects.models import Project
from vibearr_runner.delivery import DeliveryError, DeliveryReadUnavailable
from vibearr_runner.merge_github import MergeGitHub, exact_pull, merged_observation
from vibearr_runner.native_contract import digest
from work_queue.services import record_verified_merge

from .configuration import configuration_document
from .models import Run, RunnerIdentity
from .services import RunConflict, append_evidence, lock_controller
from .settings_models import RunnerSettings


def delivered(run):
    return (
        run.state == Run.State.COMPLETED
        and run.result_status == "succeeded"
        and run.native_outcome == "delivered"
        and run.active_slot is None
        and run.configuration_snapshot.get("execution_kind") == "native"
    )


def selected_credential(run, *, lock=False):
    if lock:
        lock_controller()
        RunnerSettings.objects.select_for_update().get(pk=1)
        RunnerIdentity.objects.select_for_update().get(pk=run.runner_id)
        Project.all_objects.select_for_update().get(pk=run.item.project_id)
        run.runner.refresh_from_db()
    if not delivered(run) or run.item is None or not run.runner.enabled:
        raise RunConflict(
            "Human merge observation requires a completed delivered publication."
        )
    document = configuration_document(run.runner)
    project = next(
        (
            entry
            for entry in document["projects"]
            if entry["project_id"] == run.item.project_id
        ),
        None,
    )
    workspace, _ = publication(run)
    if (
        project is None
        or project["source_policy"]["repository_id"] != workspace["repository_id"]
    ):
        raise RunConflict("Current project read authorization is unavailable.")
    reference = project["effective"]["repository_credential_id"]
    credentials = Credential.objects.select_for_update() if lock else Credential.objects
    selected = credentials.filter(
        pk=reference,
        provider="github",
        kind="token",
        revoked_at__isnull=True,
    ).first()
    if selected is None:
        raise RunConflict("Current project read credential is unavailable.")
    return selected


@sensitive_variables()
def provider(run):
    selected = selected_credential(run)
    return MergeGitHub(selected.get_secret()["secret"], read_only=True, timeout=10), (
        selected.pk,
        selected.secret_version,
    )


def ancestor(client, repository, base, head):
    value = client.request(
        "GET",
        f"/repos/{repository}/compare/{quote(base, safe='')}...{quote(head, safe='')}",
    )
    if (
        not isinstance(value, dict)
        or value.get("status") not in {"ahead", "identical"}
        or value.get("merge_base_commit", {}).get("sha") != base
        or value.get("base_commit", {}).get("sha") != base
    ):
        raise DeliveryError("Provider has not verified publication merge ancestry.")
    return {"base_sha": base, "head_sha": head, "status": value["status"]}


def publication(run):
    entry = run.evidence.filter(kind="verified_delivery").last()
    if entry is None or entry.payload.get("execution_kind") != "native":
        raise RunConflict("Delivered publication evidence is unavailable.")
    value = entry.payload
    if value["delivery"]["mode"] != "pull_request":
        raise RunConflict("Human merge observation requires PR-only delivery.")
    return value["workspace"], value["delivery"]["pull_request"]


def observe_provider(run, client):
    workspace, pull = publication(run)
    identity, _ = client.repository(repository_id=workspace["repository_id"])
    if (
        identity["repository_id"] != workspace["repository_id"]
        or identity["repository"] != workspace["repository"]
        or run.item.github_repository_id not in {None, identity["repository_id"]}
    ):
        raise DeliveryError(
            "Provider repository differs from the delivered publication."
        )
    raw = client.pull(workspace["repository"], pull["number"])
    exact_pull(raw, workspace, pull["number"])
    value = {
        "schema_version": 1,
        "status": "awaiting_review",
        "repository_id": workspace["repository_id"],
        "pull_number": pull["number"],
        "head_sha": workspace["commit_sha"],
        "base_sha": workspace["base_revision"],
        "merge_sha": "",
        "merged_at": "",
        "merged_by": None,
    }
    merged = merged_observation(raw, workspace, pull["number"])
    if merged is None:
        if raw.get("state") not in {"open", "closed"}:
            raise DeliveryError("Provider pull request state is unavailable.")
        value["status"] = (
            "closed_unmerged" if raw["state"] == "closed" else "awaiting_review"
        )
        return value
    if datetime.fromisoformat(merged["merged_at"]) > timezone.now() + timedelta(
        seconds=5
    ):
        raise DeliveryError("Provider merge timestamp is in the future.")
    actor = raw.get("merged_by")
    if (
        not isinstance(actor, dict)
        or type(actor.get("id")) is not int
        or actor["id"] < 1
        or actor.get("type") != "User"
        or not isinstance(actor.get("login"), str)
        or not actor["login"]
        or len(actor["login"]) > 100
    ):
        raise DeliveryError("Human merge requires a verified provider user identity.")
    base_sha = client.base_sha(workspace)
    ancestry = {
        "publication_base": ancestor(
            client,
            workspace["repository"],
            workspace["base_revision"],
            merged["commit_sha"],
        ),
        "current_base": ancestor(
            client, workspace["repository"], merged["commit_sha"], base_sha
        ),
    }
    confirm = client.pull(workspace["repository"], pull["number"])
    if (
        merged_observation(confirm, workspace, pull["number"]) != merged
        or confirm.get("merged_by") != actor
    ):
        raise DeliveryError("Provider merge changed during read-only reconciliation.")
    value.update(
        status="merged",
        merge_sha=merged["commit_sha"],
        merged_at=merged["merged_at"],
        merged_by={"id": actor["id"], "login": actor["login"], "type": "User"},
        ancestry_proof=ancestry,
    )
    return value


@transaction.atomic
def claim_observation(run_id, now):
    run = (
        Run.objects.select_for_update(of=("self",))
        .select_related("runner", "item")
        .get(pk=run_id)
    )
    if not delivered(run):
        return None
    cursor = run.native_status.get("publication_observation", {})
    if cursor.get("status") == "merged":
        return None
    for field in ("next_attempt_at", "lease_until"):
        if cursor.get(field) and cursor[field] > now.isoformat():
            return None
    token = str(uuid4())
    cursor.update(
        lease_token=token, lease_until=(now + timedelta(minutes=2)).isoformat()
    )
    run.native_status = {**run.native_status, "publication_observation": cursor}
    run.save(update_fields=["native_status", "updated_at"])
    return run, token


@transaction.atomic
def finish_observation(
    run_id, token, observation=None, authorization=None, *, retry_at=None
):
    selected = None
    if observation is not None:
        current = Run.objects.select_related("runner", "item").get(pk=run_id)
        selected = selected_credential(current, lock=True)
    run = (
        Run.objects.select_for_update(of=("self",))
        .select_related("runner", "item")
        .get(pk=run_id)
    )
    cursor = run.native_status.get("publication_observation", {})
    if not delivered(run) or cursor.get("lease_token") != token:
        raise RunConflict("Publication observation no longer owns its read lease.")
    now = timezone.now()
    if observation is not None and (
        not cursor.get("lease_until") or cursor["lease_until"] <= now.isoformat()
    ):
        raise RunConflict("Publication observation read lease expired.")
    if observation is not None:
        if (selected.pk, selected.secret_version) != authorization:
            raise RunConflict(
                "Current project read credential changed during observation."
            )
        previous = run.evidence.filter(kind="publication_observation").last()
        prior = (
            None
            if previous is None
            else {
                key: value
                for key, value in previous.payload.items()
                if key != "observed_at"
            }
        )
        if prior != observation:
            operation = uuid5(
                NAMESPACE_URL,
                f"publication-observation:{run.pk}:{previous.pk if previous else 0}:{digest(observation)}",
            )
            append_evidence(
                run,
                operation,
                "publication_observation",
                {**observation, "observed_at": now.isoformat()},
            )
        if observation["status"] == "merged" and run.item.source == "github":
            source = run.source_snapshot
            if (
                source.get("repository_id") != observation["repository_id"]
                or source.get("issue_number") != run.item.issue_number
            ):
                raise RunConflict("Human merge differs from the frozen source issue.")
            record_verified_merge(
                repository_id=observation["repository_id"],
                issue_number=source["issue_number"],
                pull_number=observation["pull_number"],
                merge_sha=observation["merge_sha"],
                merged_at=datetime.fromisoformat(observation["merged_at"]),
                provenance="github:human-merge:v1",
                operation_id=f"human-merge:{run.pk}",
            )
    attempts = cursor.get("attempts", 0) + 1
    delay = min(3600, 60 * 2 ** min(attempts - 1, 6)) if observation is None else 60
    next_attempt = max(now + timedelta(seconds=delay), retry_at or now)
    cursor = {
        "status": observation["status"] if observation else "blocked",
        "observed_at": now.isoformat(),
        "next_attempt_at": next_attempt.isoformat(),
        "attempts": 0 if observation else attempts,
        "error": (
            ""
            if observation
            else "Current provider read authorization or publication verification is unavailable."
        ),
    }
    run.native_status = {**run.native_status, "publication_observation": cursor}
    run.save(update_fields=["native_status", "updated_at"])
    return cursor


def observe_run(run_id):
    claimed = claim_observation(run_id, timezone.now())
    if claimed is None:
        return None
    run, token = claimed
    try:
        client, authorization = provider(run)
        observed = observe_provider(run, client)
        return finish_observation(run.pk, token, observed, authorization)
    except (
        DeliveryError,
        RunConflict,
        ValidationError,
        KeyError,
        TypeError,
        ValueError,
    ) as exc:
        retry = None
        if isinstance(exc, DeliveryReadUnavailable) and exc.retry_at:
            retry = datetime.fromtimestamp(exc.retry_at, tz=datetime_timezone.utc)
        return finish_observation(run.pk, token, retry_at=retry)


def observe_delivered_publications(*, limit=10):
    now = timezone.now().isoformat()
    due = Q(native_status__publication_observation__next_attempt_at__isnull=True) | Q(
        native_status__publication_observation__next_attempt_at__lte=now
    )
    runs = (
        Run.objects.filter(
            state=Run.State.COMPLETED,
            result_status="succeeded",
            native_outcome="delivered",
            active_slot__isnull=True,
        )
        .filter(due)
        .filter(
            Q(native_status__publication_observation__status__isnull=True)
            | ~Q(native_status__publication_observation__status="merged")
        )
        .order_by("finished_at", "pk")
    )
    return [
        observe_run(identity)
        for identity in runs.values_list("pk", flat=True)[: min(max(limit, 0), 100)]
    ]
