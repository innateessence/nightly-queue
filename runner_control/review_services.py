"""Run-owned review sessions and independently observed control publications."""

import hashlib
from datetime import timedelta
from urllib.parse import quote

from vibearr_runner import native_contract as contract
from vibearr_runner.merge_contract import merge_profile, review_report
from vibearr_runner.native_sessions import NativeSession
from vibearr_runner.review_contract import BUILTIN_ROLES, bound_report

from .merge_services import (
    latest_workspace,
    provider,
    provider_checked,
    workspace_entry,
)
from .services import RunConflict


def review_intents(run):
    return run.evidence.filter(kind="native_intent", payload__kind="review")


def publication_intents(run):
    return run.evidence.filter(kind="native_intent", payload__kind="review_publication")


def require_publications_terminal(run):
    resolved = {
        entry.payload["intent_id"]
        for entry in run.evidence.filter(
            kind__in=["native_review_publication", "native_review_publication_terminal"]
        )
    }
    expected = {str(entry.operation_id) for entry in publication_intents(run)}
    if not expected <= resolved:
        raise RunConflict(
            "Review publication outcome remains uncertain; retain the original claim."
        )


def workspace_binding(run, value):
    from .native_services import check_workspace, context

    data = value["data"]
    ctx = context(run, value["context_id"], value["context_digest"])
    workspace = workspace_entry(run, data["workspace_id"])
    if (
        latest_workspace(run).pk != workspace.pk
        or workspace.payload["attempt"] != value["attempt"]
    ):
        raise RunConflict("Native review requires the latest owned candidate revision.")
    if contract.workspace_identity(
        workspace.payload["workspace"]
    ) != contract.workspace_identity(data["workspace"]):
        raise RunConflict("Native review changed its inspected candidate identity.")
    check_workspace(run, data["workspace"], ctx, current_namespace=True)
    publication = run.evidence.filter(
        kind="native_delivery",
        payload__workspace_id=data["workspace_id"],
        payload__pull_request=data["pull_request"],
    ).last()
    if publication is None:
        raise RunConflict(
            "Native review requires its exact validated owned PR publication."
        )
    return ctx, workspace


def ci_deadline(run, profile):
    first = (
        run.evidence.filter(kind="native_delivery", payload__pull_request__isnull=False)
        .exclude(payload__pull_request=None)
        .first()
    )
    if first is None:
        raise RunConflict("Native review requires the original PR publication clock.")
    return first.created_at + timedelta(seconds=profile["ci_timeout_seconds"])


def bind_review_intent(run, value):
    from .native_services import (
        all_intents,
        authorize,
        require_prior_terminal,
        selected_account,
    )
    from django.utils import timezone

    policy = authorize(run)
    if policy.delivery_mode != "merge":
        raise RunConflict("Background AI review requires automatic merge mode.")
    _, workspace = workspace_binding(run, value)
    data = value["data"]
    session = NativeSession.from_data(data["session"])
    profile = merge_profile(run.configuration_snapshot["effective"]["merge_profile"])
    if session.account != selected_account(run) or session.manifest["run_id"] != str(
        run.pk
    ):
        raise RunConflict("Native review belongs to a different run/account.")
    ctx = run.contexts.get(pk=value["context_id"])
    if (
        session.runtime_binding_digest != ctx.document["runtime_binding_digest"]
        or session.manifest["profile_id"] != policy.runtime_profile
    ):
        raise RunConflict("Native review differs from its qualified runtime/profile.")
    reference = next(
        entry
        for entry in run.configuration_snapshot["credential_references"]
        if entry["id"] == session.account["credential_id"]
    )
    if session.auth_kind != reference["kind"] or not set(
        session.manifest["network_hosts"]
    ) <= set(policy.network_hosts):
        raise RunConflict(
            "Native review authentication/network differs from the frozen policy."
        )
    if session.manifest["source_digest"] != workspace.payload["source_digest"]:
        raise RunConflict(
            "Native review must inspect the exact committed candidate source."
        )
    original = run.evidence.get(
        kind="native_intent", operation_id=workspace.payload["agent_intent_id"]
    ).payload["data"]["session"]
    if (
        session.manifest["limits"] != original["manifest"]["limits"]
        or session.image_digest != original["image_digest"]
        or session.manifest["profile_digest"] != original["manifest"]["profile_digest"]
    ):
        raise RunConflict("Native review must preserve the original resource limits.")
    prior = list(review_intents(run))
    if len(prior) >= 2 * (min(2, policy.repair_limit) + 1):
        raise RunConflict(
            "Native review exhausted its bounded original-run operation allowance."
        )
    if any(
        entry.payload["data"]["role"] == data["role"]
        and (
            entry.payload["data"]["workspace_id"] == data["workspace_id"]
            or entry.payload["data"]["workspace"]["commit_sha"]
            == data["workspace"]["commit_sha"]
        )
        for entry in prior
    ):
        raise RunConflict(
            "Reconcile the existing paid review for this exact candidate and role."
        )
    operation = session.manifest["operation_id"]
    if any(
        operation in contract.intent_operations(entry.payload)
        for entry in all_intents(run)
    ):
        raise RunConflict("Native review cannot reuse an existing executor operation.")
    end = contract.timestamp(session.manifest["deadline"])
    maximum = min(
        run.created_at
        + timedelta(
            seconds=run.configuration_snapshot["global"]["task_timeout_seconds"]
        ),
        ci_deadline(run, profile),
        timezone.now() + timedelta(seconds=profile["review_timeout_seconds"]),
    )
    if end <= timezone.now() or end > maximum:
        raise RunConflict(
            "Native review exceeds its frozen task, CI or review deadline."
        )
    if prior and any(
        contract.timestamp(entry.payload["data"]["session"]["manifest"]["deadline"])
        != end
        for entry in prior
    ):
        raise RunConflict(
            "Native reviews must retain the original aggregate review deadline."
        )
    require_prior_terminal(run)


def bind_review(run, value):
    from .native_services import receipt_binding, saved

    intent = saved(run, value["intent_id"], "native_intent").payload
    if intent["kind"] != "review" or value["role"] != intent["data"]["role"]:
        raise RunConflict(
            "Review result does not reference its exact owned role/session."
        )
    if run.evidence.filter(
        kind="native_review", payload__intent_id=value["intent_id"]
    ).exists():
        raise RunConflict("A native review intent already has its protected result.")
    session = NativeSession.from_data(intent["data"]["session"])
    workspace = workspace_entry(run, intent["data"]["workspace_id"])
    if value["successful"] and workspace.pk != latest_workspace(run).pk:
        raise RunConflict(
            "Successful review must belong to the current owned candidate."
        )
    receipt = value["receipt"]
    receipt_binding(run, receipt, session.manifest, passed=value["successful"])
    if (
        receipt["run_id"] != str(run.pk)
        or receipt["operation_id"] != session.manifest["operation_id"]
        or value["agent_version"] != session.agent_version
    ):
        raise RunConflict("Review receipt belongs to a different native invocation.")
    if value["successful"] and (
        not receipt["container_id"]
        or receipt["source_digest"] != session.manifest["source_digest"]
        or receipt["export_digest"] != session.manifest["source_digest"]
    ):
        raise RunConflict(
            "Successful review requires the original unchanged protected source and container."
        )
    if value["successful"]:
        if len(value["report"]["findings"]) > 8:
            raise RunConflict(
                "Native review output permits at most eight bounded findings."
            )
        sessions = {
            entry.payload["session_id"]
            for entry in run.evidence.filter(
                kind__in=["native_agent", "native_review"], payload__successful=True
            )
        }
        if value["session_id"] in sessions:
            raise RunConflict(
                "Review sessions must be independent of coding and other review sessions."
            )
    bound_report(
        value, intent, run_id=str(run.pk), context_digest=intent["context_digest"]
    )


def owned_reviews(run, data):
    from .native_services import saved

    roles = {}
    for identity in data["review_ids"]:
        fact = saved(run, identity, "native_review")
        intent = saved(run, fact.payload["intent_id"], "native_intent").payload
        if (
            not fact.payload["successful"]
            or intent["data"]["workspace_id"] != data["workspace_id"]
            or intent["data"]["workspace"] != data["workspace"]
            or intent["data"]["pull_request"] != data["pull_request"]
        ):
            raise RunConflict(
                "Control reports require successful reviews of this exact candidate."
            )
        role = fact.payload["role"]
        if role in roles:
            raise RunConflict(
                "Control reports require one independent and one root review."
            )
        review_report(
            fact.payload["report"],
            role=role,
            run_id=str(run.pk),
            context_digest=intent["context_digest"],
            workspace=data["workspace"],
            pull_number=data["pull_request"]["number"],
        )
        roles[role] = fact
    if set(roles) != set(BUILTIN_ROLES):
        raise RunConflict("Control reports require both owned built-in reviews.")
    return roles


def control_files(run, data, reviews, profile):
    builtin = next(
        entry for entry in profile["reviews"] if entry["role"] == "independent"
    )
    prefix = f"{builtin['report_prefix']}{run.pk}/{data['workspace']['commit_sha']}/"
    expected = {}
    paths = {}
    for role in BUILTIN_ROLES:
        paths[role] = prefix + role + ".json"
        expected[paths[role]] = contract.canonical(reviews[role].payload["report"])
        expected[prefix + role + ".receipt.json"] = contract.canonical(
            reviews[role].payload
        )
    if data["report_paths"] != paths:
        raise RunConflict(
            "Control report paths must identify the exact owned run/candidate roles."
        )
    declared = {entry["path"]: entry["digest"] for entry in data["files"]}
    for path, content in expected.items():
        if declared.get(path) != hashlib.sha256(content).hexdigest():
            raise RunConflict(
                "Control report/receipt bytes differ from the owned protected review facts."
            )
    for path in declared.keys() - expected.keys():
        if not path.startswith(prefix) or not path.endswith(".png"):
            raise RunConflict(
                "Additional control files must be PNG screenshots under this exact candidate prefix."
            )
    return expected


@provider_checked
def provider_control_base(run, data, profile):
    client = provider(run)
    identity, _ = client.repository(repository_id=data["repository_id"])
    if (
        identity["repository"] != data["repository"]
        or client.request("GET", "/user").get("id") != profile["actor_id"]
    ):
        raise RunConflict(
            "Review publication actor/repository differs from trusted configuration."
        )
    branch = quote(data["control_base"], safe="")
    raw = client.request("GET", f"/repos/{data['repository']}/git/ref/heads/{branch}")
    if (
        raw.get("ref") != "refs/heads/" + data["control_base"]
        or raw.get("object", {}).get("sha") != data["base_revision"]
    ):
        raise RunConflict(
            "Control base is missing or changed; do not create it as a retry side effect."
        )


def bind_publication_intent(run, value):
    from .native_services import authorize, require_prior_terminal

    policy = authorize(run)
    if policy.delivery_mode != "merge":
        raise RunConflict("Review publication requires automatic merge mode.")
    workspace_binding(run, value)
    data = value["data"]
    profile = merge_profile(run.configuration_snapshot["effective"]["merge_profile"])
    repository = data["workspace"]["repository"]
    builtin = next(
        entry for entry in profile["reviews"] if entry["role"] == "independent"
    )
    if (
        data["repository_id"] != profile["repository_id"]
        or data["repository"] != repository
        or builtin["report_repository"] != repository
        or data["control_base"] != profile["control_base"]
        or data["control_base"]
        in {data["workspace"]["base_branch"], data["workspace"]["branch"]}
    ):
        raise RunConflict(
            "Control reports must use the configured same-repository control base."
        )
    expected_branch = (
        f"vibearr/review-run/{run.pk}/{data['workspace']['commit_sha'][:12]}"
    )
    if data["branch"] != expected_branch or data["commit_sha"] == data["base_revision"]:
        raise RunConflict(
            "Control publication must use its owned separate branch and new commit."
        )
    reviews = owned_reviews(run, data)
    control_files(run, data, reviews, profile)
    prior = [
        entry
        for entry in publication_intents(run)
        if entry.payload["data"]["workspace_id"] == data["workspace_id"]
    ]
    if any(
        entry.payload["data"]["publication_id"] == data["publication_id"]
        and entry.payload["data"]["workspace_id"] != data["workspace_id"]
        for entry in publication_intents(run)
    ):
        raise RunConflict(
            "Control publication identity already belongs to another candidate revision."
        )
    stable = {key: entry for key, entry in data.items() if key not in {"stage", "role"}}
    for entry in prior:
        previous = entry.payload["data"]
        if {
            key: item for key, item in previous.items() if key not in {"stage", "role"}
        } != stable:
            raise RunConflict(
                "Reconcile the immutable existing control publication identity."
            )
        if (previous["stage"], previous["role"]) == (data["stage"], data["role"]):
            raise RunConflict("Reconcile the existing control publication stage.")
    facts = run.evidence.filter(
        kind="native_review_publication", payload__publication_id=data["publication_id"]
    )
    if data["stage"] == "push":
        provider_control_base(run, data, profile)
    elif not facts.filter(payload__stage="push").exists():
        raise RunConflict("Observe the owned control push before further publication.")
    if (
        data["stage"] == "status"
        and not facts.filter(payload__stage="create_pr").exists()
    ):
        raise RunConflict(
            "Observe the owned control PR before publishing review statuses."
        )
    require_prior_terminal(run)


@provider_checked
def provider_publication(run, data, value, reviews, profile):
    from vibearr_runner.merge_github import exact_pull

    client = provider(run)
    repository = data["repository"]
    identity, _ = client.repository(repository_id=data["repository_id"])
    if (
        identity["repository"] != repository
        or client.request("GET", "/user").get("id") != profile["actor_id"]
    ):
        raise RunConflict("Provider control publication actor/repository changed.")
    commit = client.request(
        "GET", f"/repos/{repository}/git/commits/{data['commit_sha']}"
    )
    if commit.get("sha") != data["commit_sha"] or [
        entry.get("sha") for entry in commit.get("parents", [])
    ] != [data["base_revision"]]:
        raise RunConflict(
            "Control commit is not the exact single-parent owned publication."
        )
    comparison = client.request(
        "GET",
        f"/repos/{repository}/compare/{data['base_revision']}...{data['commit_sha']}",
    )
    changed = comparison.get("files")
    expected = {entry["path"]: entry["digest"] for entry in data["files"]}
    if (
        not isinstance(changed, list)
        or len(changed) != len(expected)
        or {entry.get("filename") for entry in changed} != set(expected)
        or any(entry.get("status") != "added" for entry in changed)
    ):
        raise RunConflict(
            "Provider control diff must contain only the declared new report/screenshot files."
        )
    protected = control_files(run, data, reviews, profile)
    for path, expected_digest in expected.items():
        content = client.content(repository, path, data["commit_sha"])
        if (
            hashlib.sha256(content).hexdigest() != expected_digest
            or path in protected
            and content != protected[path]
        ):
            raise RunConflict(
                "Provider control file bytes differ from the protected owned review."
            )
        if path.endswith(".png") and not content.startswith(b"\x89PNG\r\n\x1a\n"):
            raise RunConflict("Control screenshot must be an actual PNG artifact.")
    if data["stage"] == "push":
        branch = quote(data["branch"], safe="")
        raw = client.request("GET", f"/repos/{repository}/git/ref/heads/{branch}")
        if (
            raw.get("ref") != "refs/heads/" + data["branch"]
            or raw.get("object", {}).get("sha") != data["commit_sha"]
        ):
            raise RunConflict("Provider does not confirm the exact owned control ref.")
    control_pull = value["pull_request"]
    if data["stage"] == "status":
        previous = run.evidence.filter(
            kind="native_review_publication",
            payload__publication_id=data["publication_id"],
            payload__stage="create_pr",
        ).last()
        if (
            previous is None
            or control_pull is not None
            and control_pull != previous.payload["pull_request"]
        ):
            raise RunConflict(
                "Review status must retain its original owned control PR."
            )
        control_pull = previous.payload["pull_request"]
    if control_pull is not None:
        workspace = {
            **data["workspace"],
            "branch": data["branch"],
            "base_branch": data["control_base"],
            "commit_sha": data["commit_sha"],
        }
        raw = client.pull(repository, control_pull["number"])
        if (
            exact_pull(raw, workspace, control_pull["number"]) != control_pull
            or raw.get("user", {}).get("id") != profile["actor_id"]
        ):
            raise RunConflict("Control PR is not the exact owned provider publication.")
    if data["stage"] == "status":
        configured = next(
            entry for entry in profile["reviews"] if entry["role"] == data["role"]
        )
        status = value["status"]
        target = f"https://github.com/{repository}/blob/{data['commit_sha']}/{data['report_paths'][data['role']]}"
        expected_status = {
            "id": status["id"],
            "role": data["role"],
            "context": configured["context"],
            "creator_id": configured["creator_id"],
            "target_url": target,
            "state": "success",
        }
        if status != expected_status:
            raise RunConflict(
                "Review status changed its configured producer, role or immutable target."
            )
        values = client.pages(
            f"/repos/{repository}/commits/{data['workspace']['commit_sha']}/statuses"
        )
        matches = [entry for entry in values if entry.get("id") == status["id"]]
        if (
            len(matches) != 1
            or matches[0].get("context") != status["context"]
            or matches[0].get("state") != "success"
            or matches[0].get("target_url") != target
            or matches[0].get("creator", {}).get("id") != status["creator_id"]
        ):
            raise RunConflict(
                "Provider does not confirm the exact-head owned review status."
            )


def bind_publication(run, value):
    from .native_services import saved

    intent = saved(run, value["intent_id"], "native_intent").payload
    data = intent["data"]
    if (
        intent["kind"] != "review_publication"
        or value["publication_id"] != data["publication_id"]
        or value["stage"] != data["stage"]
        or value["pushed_sha"] != data["commit_sha"]
    ):
        raise RunConflict("Review publication fact differs from its immutable intent.")
    profile = merge_profile(run.configuration_snapshot["effective"]["merge_profile"])
    reviews = owned_reviews(run, data)
    provider_publication(run, data, value, reviews, profile)


def bind_publication_terminal(run, value):
    from .native_services import saved

    intent = saved(run, value["intent_id"], "native_intent").payload
    if (
        intent["kind"] != "review_publication"
        or run.evidence.filter(
            kind="native_review_publication", payload__intent_id=value["intent_id"]
        ).exists()
    ):
        raise RunConflict(
            "Only definitely unstarted unresolved review publication may be stopped."
        )


def bind_merge_reviews(run, value):
    from .native_services import saved

    data = value["data"]
    for receipt in data["receipt"]["reviews"]:
        if receipt["role"] == "external_root":
            continue
        facts = run.evidence.filter(
            kind="native_review",
            payload__role=receipt["role"],
            payload__successful=True,
        )
        fact = next(
            (entry for entry in facts if entry.payload["report"] == receipt["report"]),
            None,
        )
        if fact is None:
            raise RunConflict(
                "Merge requires both successful owned native AI review sessions."
            )
        intent = saved(run, fact.payload["intent_id"], "native_intent").payload
        if (
            intent["data"]["workspace_id"] != data["workspace_id"]
            or fact.payload["session_id"] != receipt["report"]["session_id"]
        ):
            raise RunConflict(
                "Merge review does not belong to the current candidate/session."
            )
        statuses = run.evidence.filter(
            kind="native_review_publication",
            payload__stage="status",
            payload__status__role=receipt["role"],
        )
        published = next(
            (
                entry
                for entry in statuses
                if entry.payload["status"]["id"] == receipt["status_id"]
                and entry.payload["status"]["target_url"] == receipt["report_url"]
            ),
            None,
        )
        if published is None:
            raise RunConflict(
                "Merge requires independently verified owned review publication statuses."
            )
        publication = saved(
            run, published.payload["intent_id"], "native_intent"
        ).payload["data"]
        if (
            str(fact.operation_id) not in publication["review_ids"]
            or publication["workspace_id"] != data["workspace_id"]
        ):
            raise RunConflict(
                "Merge review status belongs to a different owned review publication."
            )
