"""Owned publication revisions and provider-verified merge completion."""

from uuid import UUID
from functools import wraps

from django.utils import timezone
from django.views.decorators.debug import sensitive_variables
from django.db import transaction

from credentials.models import Credential
from vibearr_runner import native_contract as contract
from vibearr_runner.merge_contract import merge_profile, merge_receipt, review_report
from vibearr_runner.merge_github import (
    MergeGitHub,
    MergeRepair,
    MergeWait,
    exact_pull,
    merged_observation,
)
from vibearr_runner.delivery import DeliveryError

from .models import RepositoryMergeFence
from .services import RunConflict


def provider_checked(operation):
    @wraps(operation)
    def call(*args, **kwargs):
        try:
            return operation(*args, **kwargs)
        except (DeliveryError, MergeRepair, MergeWait) as exc:
            raise RunConflict(str(exc)) from None

    return call


def workspace_entry(run, operation_id):
    entry = run.evidence.filter(
        operation_id=operation_id, kind__in=["native_workspace", "native_revision"]
    ).first()
    if entry is None:
        raise RunConflict("Revision must reference an owned inspected workspace.")
    return entry


def latest_workspace(run):
    entry = run.evidence.filter(kind__in=["native_workspace", "native_revision"]).last()
    if entry is None:
        raise RunConflict("Inspect a native workspace before publication.")
    return entry


def merge_fact(run, intent_id=None):
    facts = run.evidence.filter(kind="native_merge")
    if intent_id:
        facts = facts.filter(payload__intent_id=str(intent_id))
    entry = facts.last()
    if entry is None:
        raise RunConflict("Provider-verified merge evidence is required.")
    return entry


def merge_intents(run):
    return list(run.evidence.filter(kind="native_intent", payload__kind="merge"))


def resolved_merge_intents(run):
    resolved = {
        entry.payload["intent_id"]
        for entry in run.evidence.filter(
            kind__in=["native_merge", "native_merge_terminal"]
        )
    }
    for entry in run.evidence.filter(kind="native_completion"):
        resolved.update(entry.payload["intent_ids"])
    return resolved


def require_merge_terminal(run):
    expected = {str(entry.operation_id) for entry in merge_intents(run)}
    if not expected <= resolved_merge_intents(run):
        raise RunConflict(
            "Automatic merge/reporting outcome remains uncertain; retain ownership."
        )
    if (
        run.item.source == "github"
        and run.evidence.filter(kind="native_merge").exists()
        and not run.evidence.filter(kind="native_completion").exists()
    ):
        raise RunConflict(
            "Actual merge requires completed issue reporting before releasing ownership."
        )


def receipt_binding(run, receipt, profile, workspace, pull_number, context_digest):
    merge_receipt(receipt, profile, workspace)
    sessions = set()
    for entry in receipt["reviews"]:
        report = review_report(
            entry["report"],
            role=entry["role"],
            run_id=str(run.pk),
            context_digest=context_digest,
            workspace=workspace,
            pull_number=pull_number,
        )
        if report["session_id"] in sessions:
            raise RunConflict(
                "Independent and root AI reviews require distinct sessions."
            )
        sessions.add(report["session_id"])


def completion_authority(run, document=None):
    from .native_services import authorize

    merge_fact(run)
    return authorize(run, document, completion=True)


@sensitive_variables()
def provider(run):
    from .configuration import configuration_document

    reference = run.configuration_snapshot["effective"]["repository_credential_id"]
    document = configuration_document()
    current = next(
        (
            entry
            for entry in document["projects"]
            if entry["project_id"] == run.item.project_id
        ),
        None,
    )
    if current is None or current["effective"]["repository_credential_id"] != reference:
        raise RunConflict(
            "Selected merge credential changed; retain factual reconciliation ownership."
        )
    selected = Credential.objects.filter(
        pk=reference, provider="github", kind="token", revoked_at__isnull=True
    ).first()
    metadata = next(
        (
            entry
            for entry in run.configuration_snapshot["credential_references"]
            if entry["id"] == reference
        ),
        None,
    )
    if (
        selected is None
        or metadata is None
        or metadata["secret_version"] != selected.secret_version
    ):
        raise RunConflict("Selected merge credential is unavailable or changed.")
    return MergeGitHub(selected.get_secret()["secret"], read_only=True)


@provider_checked
def provider_revision(run, value):
    client = provider(run)
    data = value["data"]
    workspace = data["workspace"]
    raw = client.pull(workspace["repository"], data["pull_request"]["number"])
    exact_pull(raw, workspace, data["pull_request"]["number"])
    if raw.get("merged") or raw.get("state") != "open":
        raise RunConflict("Only the current unmerged publication may be repaired.")
    if client.base_sha(workspace) != data["base_revision"]:
        raise RunConflict(
            "Revision base changed; observe the current base before repair."
        )


@provider_checked
def provider_authorization(run, value):
    client = provider(run)
    data = value["data"]
    workspace, pull = data["workspace"], data["pull_request"]
    raw = client.pull(workspace["repository"], pull["number"])
    exact_pull(raw, workspace, pull["number"])
    if raw.get("state") != "open" or raw.get("merged") or raw.get("draft") is not False:
        raise RunConflict("Merge requires the current open, ready pull request.")
    if (
        client.base_sha(workspace) != workspace["base_revision"]
        or raw.get("base", {}).get("sha") != workspace["base_revision"]
    ):
        raise RunConflict(
            "The base changed; create and validate a publication revision."
        )
    if raw.get("mergeable") is not True or raw.get("mergeable_state") != "clean":
        raise RunConflict(
            "Pull request is not currently mergeable under repository rules."
        )
    return {
        "policy": client.policy(workspace, data["profile"]),
        "checks": client.checks(workspace, pull["number"], data["profile"]),
        "reviews": client.reviews(
            workspace,
            pull["number"],
            data["profile"],
            run_id=str(run.pk),
            context_digest=value["context_digest"],
        ),
    }


@provider_checked
def provider_merged(run, value):
    client = provider(run)
    workspace, pull = value["workspace"], value["pull_request"]
    identity, _ = client.repository(repository_id=workspace["repository_id"])
    if identity["repository"] != workspace["repository"]:
        raise RunConflict(
            "Merged repository namespace differs from the owned publication."
        )
    observed = merged_observation(
        client.pull(workspace["repository"], pull["number"]), workspace, pull["number"]
    )
    if observed != value["merge"]:
        raise RunConflict("Provider does not confirm the exact claimed merge.")


def source_issue(run):
    source = run.source_snapshot
    number = source.get("issue_number")
    identity = source.get("metadata", {}).get("issue_id")
    if (
        type(number) is not int
        or type(identity) is not int
        or number < 1
        or identity < 1
        or run.item.source != "github"
    ):
        raise RunConflict(
            "Automatic issue completion requires the frozen GitHub source identity."
        )
    if source.get("repository_id") != run.item.github_repository_id:
        raise RunConflict("Frozen completion repository differs from the source issue.")
    return number, identity


@provider_checked
def provider_completed(run, value, merged):
    client = provider(run)
    repository = merged["workspace"]["repository"]
    number, identity = source_issue(run)
    raw = client.request("GET", f"/repos/{repository}/issues/{number}")
    if (
        raw.get("id") != identity
        or raw.get("number") != number
        or raw.get("state") != "closed"
        or "pull_request" in raw
    ):
        raise RunConflict(
            "Provider does not confirm closure of the frozen source issue."
        )
    comment = client.request(
        "GET", f"/repos/{repository}/issues/comments/{value['comment_id']}"
    )
    expected_url = f"https://api.github.com/repos/{repository}/issues/{number}"
    if (
        comment.get("id") != value["comment_id"]
        or comment.get("issue_url") != expected_url
        or value["marker"] not in comment.get("body", "")
    ):
        raise RunConflict(
            "Completion comment does not belong to this source issue/run."
        )
    profile = run.configuration_snapshot["effective"]["merge_profile"]
    if comment.get("user", {}).get("id") != profile["actor_id"]:
        raise RunConflict("Completion comment has an unexpected provider actor.")
    if value["branch_deleted"]:
        from vibearr_runner.delivery import DeliveryRejected

        path = merged["workspace"]["branch"]
        from urllib.parse import quote

        try:
            client.request(
                "GET", f"/repos/{repository}/git/ref/heads/{quote(path, safe='')}"
            )
        except DeliveryRejected as exc:
            if exc.status != 404:
                raise
        else:
            raise RunConflict("Provider still has the claimed deleted task branch.")


def publication_binding(run, value):
    from .native_services import check_workspace, context

    data = value["data"]
    ctx = context(run, value["context_id"], value["context_digest"])
    workspace = workspace_entry(run, data["workspace_id"])
    if (
        latest_workspace(run).pk != workspace.pk
        or workspace.payload["attempt"] != value["attempt"]
    ):
        raise RunConflict(
            "Automatic delivery requires the latest inspected candidate revision."
        )
    if contract.workspace_identity(
        workspace.payload["workspace"]
    ) != contract.workspace_identity(data["workspace"]):
        raise RunConflict("Automatic delivery changed its inspected candidate.")
    check_workspace(run, data["workspace"], ctx, current_namespace=True)
    fact = run.evidence.filter(
        kind="native_delivery",
        payload__publication_id=data["publication_id"],
        payload__workspace_id=data["workspace_id"],
    ).last()
    if fact is None or fact.payload["pull_request"] != data["pull_request"]:
        raise RunConflict("Automatic delivery requires its known owned pull request.")
    return ctx, workspace


def bind_revision_intent(run, value):
    from .native_services import authorize, require_prior_terminal

    policy = authorize(run)
    if policy.delivery_mode != "merge":
        raise RunConflict("Publication revisions require automatic merge mode.")
    data = value["data"]
    previous = workspace_entry(run, data["workspace_id"])
    revised = dict(value, attempt=previous.payload["attempt"])
    publication_binding(run, revised)
    if value["attempt"] != previous.payload["attempt"] + 1 or value["attempt"] > min(
        2, policy.repair_limit
    ):
        raise RunConflict(
            "Publication revisions consume the consecutive shared repair allowance."
        )
    if run.evidence.filter(kind="native_merge").exists() or any(
        str(entry.operation_id) not in resolved_merge_intents(run)
        for entry in merge_intents(run)
    ):
        raise RunConflict(
            "A merge intent already owns this publication; reconcile it before any repair."
        )
    if run.evidence.filter(
        kind="native_intent",
        payload__kind="revision",
        payload__attempt=value["attempt"],
    ).exists():
        raise RunConflict("Reconcile the existing immutable publication revision.")
    if (
        data["reason"] == "base_update"
        and data["base_revision"] == data["workspace"]["base_revision"]
    ):
        raise RunConflict("Base update must observe a new base commit.")
    if (
        data["reason"] == "ci_repair"
        and data["base_revision"] != data["workspace"]["base_revision"]
    ):
        raise RunConflict("CI-only repair must preserve its inspected base.")
    require_prior_terminal(run)
    provider_revision(run, value)


def bind_revision(run, value):
    from .native_services import saved

    intent = saved(run, value["intent_id"], "native_intent").payload
    data = intent["data"]
    previous = workspace_entry(run, value["previous_workspace_id"])
    if intent["kind"] != "revision" or data["reason"] != "base_update":
        raise RunConflict("Base revision requires its immutable base-update intent.")
    expected = {
        "context_id": intent["context_id"],
        "context_digest": intent["context_digest"],
        "attempt": intent["attempt"],
        "previous_workspace_id": data["workspace_id"],
        "agent_intent_id": previous.payload["agent_intent_id"],
        "export_digest": previous.payload["export_digest"],
        "parents": [previous.payload["workspace"]["commit_sha"], data["base_revision"]],
    }
    if (
        any(value[key] != entry for key, entry in expected.items())
        or previous.pk != latest_workspace(run).pk
    ):
        raise RunConflict(
            "Base revision differs from its saved ancestry/session attribution."
        )
    expected_workspace = {
        key: entry
        for key, entry in data["workspace"].items()
        if key not in {"base_revision", "commit_sha", "tree_sha"}
    }
    if (
        any(
            value["workspace"][key] != entry
            for key, entry in expected_workspace.items()
        )
        or value["workspace"]["base_revision"] != data["base_revision"]
    ):
        raise RunConflict(
            "Base revision changed repository, branch or observed base identity."
        )
    if value["workspace"]["commit_sha"] in value["parents"]:
        raise RunConflict("Base revision must produce a new normal merge commit.")
    if run.evidence.filter(
        kind="native_revision", payload__intent_id=value["intent_id"]
    ).exists():
        raise RunConflict("Revision intent already has an inspected result.")


def bind_merge_intent(run, value, operation_id):
    from .native_services import authorize, require_prior_terminal

    data = value["data"]
    if operation_id is None:
        raise RunConflict(
            "Merge authorization requires its immutable metadata operation ID."
        )
    if data["stage"] != "merge":
        completion_authority(run)
        merged = merge_fact(run).payload
        original = run.evidence.get(
            operation_id=merged["intent_id"], kind="native_intent"
        ).payload
        if any(
            data[key] != original["data"][key]
            for key in (
                "publication_id",
                "workspace_id",
                "workspace",
                "pull_request",
                "profile",
                "receipt",
            )
        ):
            raise RunConflict(
                "Completion must retain the original verified merge identity."
            )
        if data["stage"] == "delete" and not data["profile"]["delete_branch"]:
            raise RunConflict("Branch deletion is not configured for this publication.")
        if any(
            entry.payload["data"]["stage"] == data["stage"]
            for entry in merge_intents(run)
        ):
            raise RunConflict("Reconcile the existing completion mutation.")
        return
    policy = authorize(run)
    if policy.delivery_mode != "merge":
        raise RunConflict("Automatic merge is not enabled for this run.")
    ctx, _ = publication_binding(run, value)
    profile = merge_profile(run.configuration_snapshot["effective"]["merge_profile"])
    if data["profile"] != profile:
        raise RunConflict("Merge intent changed its frozen trusted profile.")
    receipt_binding(
        run,
        data["receipt"],
        profile,
        data["workspace"],
        data["pull_request"]["number"],
        ctx.digest,
    )
    if run.evidence.filter(kind="native_merge").exists() or any(
        str(entry.operation_id) not in resolved_merge_intents(run)
        for entry in merge_intents(run)
    ):
        raise RunConflict("Reconcile the existing automatic merge mutation.")
    require_prior_terminal(run)
    observed = provider_authorization(run, value)
    if observed != data["receipt"]:
        raise RunConflict(
            "Current provider checks/reviews differ from the merge receipt."
        )
    reserve_fence(run, data, operation_id)


@transaction.atomic
def reserve_fence(run, data, operation_id):
    values = dict(
        run=run,
        publication_id=UUID(data["publication_id"]),
        intent_id=operation_id,
        head_sha=data["workspace"]["commit_sha"],
        base_sha=data["workspace"]["base_revision"],
        state="pending",
    )
    fence, created = RepositoryMergeFence.objects.select_for_update().get_or_create(
        repository_id=data["workspace"]["repository_id"], defaults=values
    )
    if created:
        return fence
    if fence.state == "pending":
        raise RunConflict("Another pending merge owns this repository fence.")
    for key, value in values.items():
        setattr(fence, key, value)
    fence.save(update_fields=[*values, "updated_at"])
    return fence


def bind_merge(run, value):
    from .native_services import saved
    from work_queue.services import record_verified_merge

    intent = saved(run, value["intent_id"], "native_intent").payload
    if intent["kind"] != "merge" or intent["data"]["stage"] != "merge":
        raise RunConflict("Merge fact requires its exact merge-stage intent.")
    for key in (
        "workspace_id",
        "publication_id",
        "workspace",
        "pull_request",
        "receipt",
    ):
        if value[key] != intent["data"][key]:
            raise RunConflict("Merge fact differs from its immutable authorization.")
    fence = (
        RepositoryMergeFence.objects.select_for_update()
        .filter(
            repository_id=value["workspace"]["repository_id"],
            run=run,
            intent_id=value["intent_id"],
            state="pending",
        )
        .first()
    )
    if fence is None:
        raise RunConflict(
            "The original merge intent does not own this repository fence."
        )
    provider_merged(run, value)
    if run.item.source == "github":
        number, _ = source_issue(run)
        record_verified_merge(
            repository_id=value["workspace"]["repository_id"],
            issue_number=number,
            pull_number=value["pull_request"]["number"],
            merge_sha=value["merge"]["commit_sha"],
            merged_at=contract.timestamp(value["merge"]["merged_at"]),
            provenance="github:merge:v1",
            operation_id=f"native-merge:{run.pk}:{value['publication_id']}",
        )


def bind_completion(run, value):
    from .native_services import saved

    merged = merge_fact(run, value["merge_intent_id"]).payload
    number, identity = source_issue(run)
    if (value["issue_number"], value["issue_id"]) != (number, identity) or value[
        "marker"
    ] != f"<!-- vibearr-run:{run.pk}:complete -->":
        raise RunConflict(
            "Completion must target this frozen source issue and run marker."
        )
    stages = []
    for identity in value["intent_ids"]:
        intent = saved(run, identity, "native_intent").payload
        if (
            intent["kind"] != "merge"
            or intent["data"]["publication_id"] != merged["publication_id"]
            or intent["data"]["workspace"] != merged["workspace"]
        ):
            raise RunConflict("Completion references a different merge publication.")
        stages.append(intent["data"]["stage"])
    expected = {"comment", "close"} | ({"delete"} if value["branch_deleted"] else set())
    if len(stages) != len(set(stages)) or set(stages) != expected:
        raise RunConflict(
            "Completion must resolve the exact comment/close/cleanup intents."
        )
    provider_completed(run, value, merged)
    RepositoryMergeFence.objects.filter(
        repository_id=merged["workspace"]["repository_id"],
        run=run,
        intent_id=value["merge_intent_id"],
    ).update(state="completed", updated_at=timezone.now())


def bind_merge_terminal(run, value):
    from .native_services import saved

    intent = saved(run, value["intent_id"], "native_intent").payload
    if intent["kind"] != "merge" or value["intent_id"] in resolved_merge_intents(run):
        raise RunConflict(
            "Only an unresolved, definitely unstarted merge mutation may be stopped."
        )
    if intent["data"]["stage"] == "merge":
        RepositoryMergeFence.objects.filter(
            repository_id=intent["data"]["workspace"]["repository_id"],
            run=run,
            intent_id=value["intent_id"],
        ).update(state="not_started", updated_at=timezone.now())


def validate_merged_delivery(run, value, workspace):
    if latest_workspace(run).pk != workspace.pk:
        raise RunConflict(
            "Merge success must use the latest inspected publication revision."
        )
    merged = merge_fact(run).payload
    delivery = value["delivery"]
    if (
        any(merged[key] != value[key] for key in ("workspace",))
        or merged["pull_request"] != delivery["pull_request"]
    ):
        raise RunConflict("Merge success changed the provider-verified publication.")
    expected_merge = {key: merged["merge"][key] for key in ("commit_sha", "head_sha")}
    checks = [
        {
            key: entry[key]
            for key in ("name", "producer_id", "head_sha", "conclusion", "url")
        }
        for entry in merged["receipt"]["checks"]
    ]
    if delivery["merge"] != expected_merge or delivery["checks"] != checks:
        raise RunConflict(
            "Merge success must retain its exact verified checks and merge SHA."
        )
    if run.item.source != "github":
        if delivery["issue_completion"] is not None:
            raise RunConflict("Manual tasks cannot claim GitHub issue completion.")
        RepositoryMergeFence.objects.filter(
            repository_id=merged["workspace"]["repository_id"],
            run=run,
            intent_id=merged["intent_id"],
        ).update(state="completed", updated_at=timezone.now())
        return
    completion = run.evidence.filter(
        kind="native_completion", payload__merge_intent_id=merged["intent_id"]
    ).last()
    if completion is None:
        raise RunConflict("Merged issue reporting must complete before run success.")
    close_intent = next(
        identity
        for identity in completion.payload["intent_ids"]
        if run.evidence.get(operation_id=identity, kind="native_intent").payload[
            "data"
        ]["stage"]
        == "close"
    )
    expected = {
        "operation_id": close_intent,
        "issue_number": completion.payload["issue_number"],
        "state": "closed",
    }
    if delivery["issue_completion"] != expected:
        raise RunConflict(
            "Merge success differs from its acknowledged issue completion."
        )
