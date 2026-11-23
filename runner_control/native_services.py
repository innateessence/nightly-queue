"""Run-scoped native protocol authorization and immutable receipt binding."""

from datetime import timedelta
from functools import wraps

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from credentials.redaction import redact_run_payload
from projects.models import Project
from vibearr_runner import native_contract as contract
from vibearr_runner.policy import ExecutionPolicy
from work_queue.models import QueueItem
from work_queue.services import (
    claimable_items,
    native_delivery_exists,
    reset_retry_item,
    source_wait_reason,
)

from .configuration import configuration_document
from .models import Run
from .services import RunConflict, lock_controller, owned_run, verify_active


def checked(operation):
    @wraps(operation)
    def call(*args, **kwargs):
        try:
            return operation(*args, **kwargs)
        except (
            contract.NativeContractError,
            KeyError,
            TypeError,
            ValueError,
            AttributeError,
        ) as exc:
            raise ValidationError(
                {"native": str(exc) or "Native metadata is invalid."}
            ) from None

    return call


def registered(run):
    return run.evidence.filter(kind="native_intent").exists()


def prior_delivery(item):
    return native_delivery_exists(item)


def known_delivery_url(run):
    for entry in run.evidence.filter(kind="native_delivery").order_by("-pk"):
        if entry.payload["pull_request"]:
            return entry.payload["pull_request"]["url"]
    return ""


def complete_retry(run):
    if prior_delivery(run.item):
        run.wait_reason = "Prior native delivery requires reconciliation before retry."
        run.item.error_summary = run.wait_reason
        run.item.save(update_fields=["error_summary"])
        run.save(update_fields=["wait_reason", "updated_at"])
        return
    reset_retry_item(run.item)


def native_policy(run):
    if run.configuration_snapshot.get("execution_kind") != "native":
        raise RunConflict("This run does not use native execution.")
    policy = ExecutionPolicy.from_data(run.configuration_snapshot["execution_policy"])
    if policy.digest != run.configuration_snapshot.get("policy_digest"):
        raise RunConflict("Native policy snapshot is invalid.")
    return policy


def selected_account(run):
    effective = run.configuration_snapshot["effective"]
    reference = effective["implementation_credential_id"]
    metadata = next(
        (
            entry
            for entry in run.configuration_snapshot["credential_references"]
            if entry["id"] == reference
        ),
        None,
    )
    if metadata is None:
        raise RunConflict("Native account metadata is missing.")
    return {
        "backend": effective["implementation_backend"],
        "credential_id": reference,
        "credential_version": metadata["secret_version"],
        "model": effective["implementation_model"],
    }


def lock_scope(run, claim_token, *, reporting=False):
    document = configuration_document(run.runner)
    project_ids = {
        run.item.project_id,
        run.configuration_snapshot.get("project_id", run.item.project_id),
    }
    repository_ids = {
        run.item.project.github_repository_id,
        run.configuration_snapshot.get("source_policy", {}).get("repository_id"),
    } - {None}
    project_ids.update(
        Project.objects.filter(github_repository_id__in=repository_ids).values_list(
            "pk", flat=True
        )
    )
    projects = {
        entry.pk: entry
        for entry in Project.objects.select_for_update()
        .filter(pk__in=project_ids)
        .order_by("pk")
    }
    item = QueueItem.objects.select_for_update().get(pk=run.item_id)
    current = Run.objects.select_for_update().get(pk=run.pk)
    if item.project_id not in projects:
        raise RunConflict("Project changed during native authorization.")
    item.project = projects[item.project_id]
    current.item = item
    verify_active(current, claim_token, allow_cancellation=reporting)
    native_policy(current)
    return current, document


def authorize(run, document=None, *, completion=False):
    if completion and not run.evidence.filter(kind="native_merge").exists():
        raise RunConflict("Completion authority requires a provider-verified merge.")
    policy = native_policy(run)
    document = document or configuration_document(run.runner)
    if run.configuration_snapshot.get("runner_configuration_id", 1) != document.get(
        "runner_configuration_id", 1
    ):
        raise RunConflict("Runner configuration assignment changed; retry explicitly.")
    controls = document["global"]
    if not completion and (not controls["enabled"] or controls["paused"]):
        raise RunConflict("Native execution is disabled or paused.")
    project = next(
        (
            entry
            for entry in document["projects"]
            if entry["project_id"] == run.item.project_id
        ),
        None,
    )
    if project is None or project["errors"]:
        raise RunConflict("Native project configuration is unavailable.")
    if (
        project["project_id"] != run.configuration_snapshot["project_id"]
        or project["policy_digest"] != policy.digest
    ):
        raise RunConflict(
            "Native project or execution policy changed; retry explicitly."
        )
    effective = run.configuration_snapshot["effective"]
    current = project["effective"]
    if current.get("merge_profile", {}) != effective.get("merge_profile", {}):
        raise RunConflict("Trusted merge profile changed; stop and reconcile the run.")
    for key in (
        "implementation_backend",
        "implementation_model",
        "implementation_credential_id",
        "issue_credential_id",
        "repository_credential_id",
    ):
        if current.get(key) != effective.get(key):
            raise RunConflict(
                "Native account or credential selection changed; retry explicitly."
            )
    versions = {entry["id"]: entry for entry in document["credentials"]}
    for entry in run.configuration_snapshot["credential_references"]:
        if entry["id"] not in {
            effective["implementation_credential_id"],
            effective["issue_credential_id"],
            effective["repository_credential_id"],
        }:
            continue
        actual = versions.get(entry["id"])
        if (
            not actual
            or actual.get("revoked_at")
            or actual["secret_version"] != entry["secret_version"]
        ):
            raise RunConflict(
                "Native selected credential changed or was revoked; retry explicitly."
            )
    if completion:
        return policy
    reason = source_wait_reason(run.item)
    if reason:
        raise RunConflict(reason)
    frozen = run.source_snapshot
    actual = (
        run.item.source_metadata
        if run.item.source == "github"
        else {"title": run.item.title, "body": run.item.instructions}
    )
    original = frozen.get("metadata", {}) if run.item.source == "github" else frozen
    if actual.get("title") != original.get("title", frozen.get("title")) or actual.get(
        "body", ""
    ) != original.get("body", frozen.get("body", "")):
        raise RunConflict("Issue instructions changed; retry explicitly.")
    if (
        not registered(run)
        and run.item.source == "github"
        and (
            run.item.issue_revision != frozen.get("revision")
            or "comments" not in frozen
        )
    ):
        raise RunConflict(
            "Refresh the strict complete source before initial native dispatch."
        )
    return policy


@transaction.atomic
def candidates(runner):
    lock_controller(runner)
    document = configuration_document(runner)
    if not document["global"]["enabled"] or document["global"]["paused"]:
        return {
            "schema_version": 1,
            "configuration_revision": document["revision"],
            "project_ids": [],
        }
    valid = {
        entry["project_id"] for entry in document["projects"] if not entry["errors"]
    }
    project_ids = sorted(
        set(
            claimable_items()
            .filter(project_id__in=valid)
            .values_list("project_id", flat=True)
        )
    )
    return {
        "schema_version": 1,
        "configuration_revision": document["revision"],
        "project_ids": project_ids[:1000],
    }


@transaction.atomic
@checked
def update_status(*, runner, run_id, claim_token, data):
    lock_controller(runner)
    run = owned_run(runner, run_id)
    verify_active(run, claim_token, allow_cancellation=True)
    native_policy(run)
    value = contract.status_document(**data)
    value = redact_run_payload(run, value)
    contract.status_document(**value)
    previous = run.native_status
    if previous.get("sequence", 0) > value["sequence"]:
        return run
    if previous.get("sequence") == value["sequence"]:
        if previous != value:
            raise RunConflict(
                "Native status sequence already contains different content."
            )
        return run
    if value["repair_count"] < previous.get("repair_count", 0) or value[
        "repair_count"
    ] > min(2, native_policy(run).repair_limit):
        raise RunConflict("Native repair accounting cannot reset or exceed the policy.")
    run.native_status = value
    run.heartbeat_at = timezone.now()
    run.stage = value["stage"]
    run.wait_reason = value["wait_reason"]
    run.save(
        update_fields=[
            "native_status",
            "heartbeat_at",
            "stage",
            "wait_reason",
            "updated_at",
        ]
    )
    return run


def saved(run, operation_id, kind):
    entry = run.evidence.filter(operation_id=operation_id, kind=kind).first()
    if entry is None:
        raise RunConflict("Native metadata does not reference an owned saved fact.")
    return entry


def context(run, context_id, context_digest):
    entry = run.contexts.filter(pk=context_id, digest=context_digest).first()
    if entry is None:
        raise RunConflict("Native context is not an owned immutable version.")
    contract.validate_context(entry.document)
    return entry


def latest_context(run):
    entry = run.contexts.filter(pk=run.context_reference).first()
    if entry is None:
        raise RunConflict("Save a native context before execution.")
    contract.validate_context(entry.document)
    return entry


def check_workspace(run, value, ctx, *, current_namespace=False):
    contract.validate_workspace(value)
    source = ctx.document["repository"]
    expected = {
        "repository_id": source["repository_id"],
        "branch": source["branch"],
        "base_branch": source["base_branch"],
    }
    if any(value[key] != entry for key, entry in expected.items()):
        raise RunConflict("Native workspace differs from the claimed repository/base.")
    if value["base_revision"] != source["base_revision"]:
        from .merge_services import workspace_entry

        lineage = run.evidence.filter(
            kind="native_revision",
            payload__context_id=str(ctx.pk),
            payload__context_digest=ctx.digest,
            payload__workspace__base_revision=value["base_revision"],
        ).last()
        if lineage is None:
            raise RunConflict(
                "Changed base requires an immutable publication revision."
            )
        parent = workspace_entry(run, lineage.payload["previous_workspace_id"])
        if lineage.payload["parents"] != [
            parent.payload["workspace"]["commit_sha"],
            value["base_revision"],
        ]:
            raise RunConflict(
                "Changed base differs from its saved publication ancestry."
            )
    if current_namespace:
        current = run.item.project
        expected_name = current.repository_url.removeprefix(
            "https://github.com/"
        ).removesuffix(".git")
        if (
            current.github_repository_id != value["repository_id"]
            or value["repository"] != expected_name
        ):
            raise RunConflict(
                "Resolve the current canonical repository by stable ID before publication."
            )


@checked
def validate_context_binding(run, value, base_commit, resolved_base_branch):
    document = contract.validate_context(value)
    native_policy(run)
    if document["run_id"] != str(run.pk) or document["source"] != run.source_snapshot:
        raise RunConflict("Native context must preserve this run's immutable source.")
    if document["policy_digest"] != run.configuration_snapshot[
        "policy_digest"
    ] or document["account"] != selected_account(run):
        raise RunConflict("Native context differs from the frozen policy/account.")
    selected = document["repository"]
    if (
        selected["base_revision"] != base_commit
        or selected["base_branch"] != resolved_base_branch
        or selected["branch"] != run.item.branch_name
    ):
        raise RunConflict("Native context differs from the inspected checkout.")
    if (
        not run.item.project.github_repository_id
        or selected["repository_id"] != run.item.project.github_repository_id
    ):
        raise RunConflict(
            "Observe and bind the stable repository before saving context."
        )
    if selected["repository_url"] != run.item.project.repository_url:
        raise RunConflict(
            "Resolve the current canonical repository before saving context."
        )
    if document["source"].get("repository_id") != selected["repository_id"]:
        raise RunConflict(
            "Native source does not contain the verified repository identity."
        )
    if run.item.base_branch and resolved_base_branch != run.item.base_branch:
        raise RunConflict("Native context changed the frozen base branch.")
    policy = native_policy(run)
    if document["validation_profile"]["profile_id"] != policy.validation_profile:
        raise RunConflict("Native context selected a different validation recipe.")
    if registered(run):
        require_termination(run)
        previous = latest_context(run)
        for key in ("source", "policy_digest", "account", "validation_profile"):
            if previous.document[key] != document[key]:
                raise RunConflict(
                    "An active native attempt cannot rewrite frozen instructions/policy."
                )
    if not run.item.base_branch:
        QueueItem.objects.filter(pk=run.item_id, base_branch="").update(
            base_branch=resolved_base_branch
        )
        run.item.base_branch = resolved_base_branch
    return document


@checked
def validate_plan_binding(run, value, ctx):
    document = contract.validate_plan(value)
    if (
        document["context_id"] != str(ctx.pk)
        or document["context_digest"] != ctx.digest
    ):
        raise RunConflict("Native plan must reference the current immutable context.")
    if document["acceptance_criteria"] != ctx.document["acceptance_criteria"]:
        raise RunConflict("Native plan must retain the frozen acceptance criteria.")
    return document


def all_intents(run):
    return list(run.evidence.filter(kind="native_intent"))


def resolved(run):
    operations, publications = set(), set()
    for entry in run.evidence.filter(kind="native_agent"):
        operations.add(entry.payload["receipt"]["operation_id"])
    for entry in run.evidence.filter(kind="native_validation"):
        operations.update(
            check["receipt"]["operation_id"] for check in entry.payload["checks"]
        )
    for entry in run.evidence.filter(kind="native_intent"):
        if (
            entry.payload["kind"] == "validation"
            and entry.payload["data"]["provenance"]
        ):
            operations.add(entry.payload["data"]["provenance"]["build_id"])
    for entry in run.evidence.filter(kind="native_delivery"):
        for identity in entry.payload["intent_ids"]:
            intent = saved(run, identity, "native_intent").payload
            if (
                intent["data"]["stage"] == "push"
                or entry.payload["pull_request"] is not None
            ):
                publications.add(identity)
    for entry in run.evidence.filter(kind="native_termination"):
        operations.update(
            value["operation_id"] for value in entry.payload["operations"]
        )
        publications.update(
            value["intent_id"] for value in entry.payload["publications"]
        )
    return operations, publications


def require_prior_terminal(run, *, ready_build=None):
    from .merge_services import require_merge_terminal

    require_merge_terminal(run)
    operations, publications = resolved(run)
    if ready_build:
        operations.add(ready_build)
    for entry in all_intents(run):
        if entry.payload["kind"] == "delivery":
            if str(entry.operation_id) not in publications:
                raise RunConflict("Prior publication outcome remains ambiguous.")
        elif not set(contract.intent_operations(entry.payload)) <= operations:
            raise RunConflict("Prior native operation remains active or uncertain.")


def deadline_and_limits(run, manifest, ceiling, *, stage):
    policy = native_policy(run)
    desired = {
        "cpu": policy.cpu_limit,
        "memory_mb": policy.memory_mb,
        "pids": policy.pids_limit,
        "workspace_mb": policy.workspace_mb,
    }
    expected = {key: min(value, ceiling[key]) for key, value in desired.items()}
    if manifest["limits"] != expected:
        raise RunConflict(
            "Native manifest must enforce the frozen policy resource limits."
        )
    end = contract.timestamp(manifest["deadline"])
    maximum = run.created_at + timedelta(
        seconds=run.configuration_snapshot["global"]["task_timeout_seconds"]
    )
    allowance = run.configuration_snapshot["global"][f"{stage}_timeout_seconds"]
    maximum = min(maximum, timezone.now() + timedelta(seconds=allowance))
    if end > maximum or end <= timezone.now():
        raise RunConflict(
            "Native manifest exceeds or exhausted the frozen task/stage deadline."
        )
    prior = []
    for entry in all_intents(run):
        kind = entry.payload["kind"]
        data = entry.payload["data"]
        if stage == "implementation" and kind == "agent":
            prior.append(contract.timestamp(data["session"]["manifest"]["deadline"]))
        if stage == "validation" and kind == "validation":
            prior.append(
                contract.timestamp(data["operations"][0]["manifest"]["deadline"])
            )
    if prior and end > min(prior):
        raise RunConflict("Native repairs cannot extend the original stage deadline.")


def workspace_fact(run, operation_id, ctx):
    from .merge_services import workspace_entry

    entry = workspace_entry(run, operation_id)
    value = entry.payload
    if value["context_id"] != str(ctx.pk) or value["context_digest"] != ctx.digest:
        raise RunConflict("Native workspace belongs to a different context.")
    check_workspace(run, value["workspace"], ctx)
    return entry


def failed_local_validation(run, workspace_id, attempt):
    intent = run.evidence.filter(
        kind="native_intent",
        payload__kind="validation",
        payload__attempt=attempt,
        payload__data__workspace_id=workspace_id,
    ).last()
    if intent is None:
        return False
    result = run.evidence.filter(
        kind="native_validation", payload__intent_id=str(intent.operation_id)
    ).last()
    return result is not None and result.payload["state"] == "failed"


def validation_profile_binding(run, value, workspace, ctx):
    selected = contract.profile_from_reference(value["profile"])
    if selected.dependency_digest != workspace["requirements_digest"]:
        raise RunConflict(
            "Validation image does not bind the committed candidate requirements."
        )
    parent = contract.profile_from_reference(ctx.document["validation_profile"])
    proof = value.get("provenance")
    if selected.digest == parent.digest:
        if proof is not None:
            raise RunConflict(
                "The configured validation image cannot claim a derived build."
            )
        return None
    from vibearr_executor.validation_profile import validate_dependency_provenance

    validate_dependency_provenance(proof, selected, parent=parent, run_id=str(run.pk))
    build = next(
        (
            entry
            for entry in all_intents(run)
            if entry.payload["kind"] == "dependency"
            and entry.payload["data"]["build_id"] == proof["build_id"]
        ),
        None,
    )
    if build is None:
        raise RunConflict(
            "Derived validation lacks a registered owned dependency build."
        )
    source = workspace_fact(run, build.payload["data"]["workspace_id"], ctx).payload
    if (
        proof["source_digest"] != source["source_digest"]
        or source["requirements_digest"] != workspace["requirements_digest"]
        or proof["parent_runtime_binding"] != ctx.document["runtime_binding_digest"]
    ):
        raise RunConflict(
            "Derived validation provenance differs from its owned source/runtime."
        )
    return proof["build_id"]


def validate_intent_binding(run, value, operation_id=None):
    from .factory_delivery import validate_delivery_policy

    validate_delivery_policy(run, value)
    ctx = context(run, value["context_id"], value["context_digest"])
    if str(ctx.pk) != run.context_reference:
        raise RunConflict("Native dispatch requires the current context.")
    plan = run.plans.filter(pk=run.plan_reference, context=ctx).first()
    if plan is None:
        raise RunConflict("Save a native plan artifact before dispatch.")
    contract.validate_plan(plan.document)
    from .merge_services import (
        bind_merge_intent,
        bind_revision_intent,
        completion_authority,
    )

    reporting = value["kind"] == "merge" and value["data"]["stage"] != "merge"
    policy = completion_authority(run) if reporting else authorize(run)
    if value["policy_digest"] != policy.digest or value["attempt"] > min(
        2, policy.repair_limit
    ):
        raise RunConflict("Native dispatch exceeds its frozen policy/repair limit.")
    kind, data = value["kind"], value["data"]
    if kind == "agent" and hasattr(run, "delivery_recovery"):
        raise RunConflict("Delivery-only recovery cannot dispatch another agent.")
    if kind == "revision":
        bind_revision_intent(run, value)
        return
    if kind == "merge":
        bind_merge_intent(run, value, operation_id)
        return
    ready_build = None
    if kind == "agent":
        from vibearr_executor.contracts import Limits
        from vibearr_runner.native_sessions import NativeSession

        session = NativeSession.from_data(data["session"])
        if session.account != selected_account(run) or session.manifest[
            "run_id"
        ] != str(run.pk):
            raise RunConflict("Native session belongs to another run/account.")
        if (
            session.runtime_binding_digest != ctx.document["runtime_binding_digest"]
            or session.manifest["profile_id"] != policy.runtime_profile
        ):
            raise RunConflict(
                "Native session differs from its qualified runtime/profile."
            )
        reference = next(
            entry
            for entry in run.configuration_snapshot["credential_references"]
            if entry["id"] == session.account["credential_id"]
        )
        if session.auth_kind != reference["kind"] or not set(
            session.manifest["network_hosts"]
        ) <= set(policy.network_hosts):
            raise RunConflict(
                "Native session authentication/network differs from the policy."
            )
        prior = [
            entry.payload["attempt"]
            for entry in all_intents(run)
            if entry.payload["kind"] in {"agent", "revision"}
        ]
        revisions = run.evidence.filter(
            kind="native_intent",
            payload__kind="revision",
            payload__attempt=value["attempt"],
            payload__data__reason="ci_repair",
        )
        repair_revision = revisions.last()
        expected_attempt = (
            max(prior, default=-1) if repair_revision else max(prior, default=-1) + 1
        )
        if value["attempt"] != expected_attempt or any(
            entry.payload["kind"] == "agent"
            and entry.payload["attempt"] == value["attempt"]
            for entry in all_intents(run)
        ):
            raise RunConflict(
                "Native agent attempts cannot skip or repeat an invocation."
            )
        expected_source = ctx.document["source_digest"]
        if value["attempt"]:
            from .merge_services import latest_workspace

            previous_entry = workspace_fact(run, data["workspace_id"], ctx)
            previous = previous_entry.payload
            if previous_entry.pk != latest_workspace(run).pk:
                raise RunConflict(
                    "Native repair requires the latest inspected workspace."
                )
            if previous["attempt"] != value["attempt"] - 1:
                raise RunConflict(
                    "Native repair must use the preceding inspected workspace."
                )
            expected_source = previous["source_digest"]
            if run.evidence.filter(kind="native_delivery").exists():
                published = run.evidence.filter(
                    kind="native_delivery", payload__workspace_id=data["workspace_id"]
                ).exists()
                explicit_revision = (
                    repair_revision is not None
                    and repair_revision.payload["data"]["workspace_id"]
                    == data["workspace_id"]
                )
                if published and not explicit_revision:
                    raise RunConflict(
                        "Published CI repair requires its explicit publication revision intent."
                    )
                if not published and not failed_local_validation(
                    run, data["workspace_id"], previous["attempt"]
                ):
                    raise RunConflict(
                        "Unpublished candidate repair requires its latest failed local validation."
                    )
        elif data["workspace_id"] is not None:
            raise RunConflict("Initial native work starts from the inspected base.")
        if session.manifest["source_digest"] != expected_source:
            raise RunConflict(
                "Native invocation source differs from its inspected tree."
            )
        ceiling = {
            "cpu": policy.cpu_limit,
            "memory_mb": policy.memory_mb,
            "pids": policy.pids_limit,
            "workspace_mb": policy.workspace_mb,
        }
        Limits.parse(ceiling)
        deadline_and_limits(run, session.manifest, ceiling, stage="implementation")
    else:
        workspace = workspace_fact(run, data["workspace_id"], ctx).payload
        if workspace["attempt"] != value["attempt"]:
            raise RunConflict(
                "Native phase attempt differs from its inspected workspace."
            )
        if kind == "validation":
            if (
                data["run_id"] != str(run.pk)
                or data["tree_sha"] != workspace["workspace"]["tree_sha"]
                or data["source_digest"] != workspace["source_digest"]
            ):
                raise RunConflict(
                    "Native validation differs from the inspected committed tree."
                )
            ready_build = validation_profile_binding(run, data, workspace, ctx)
            profile = contract.profile_from_reference(data["profile"])
            for operation, fixed in zip(
                data["operations"], profile.executor_profiles()
            ):
                deadline_and_limits(
                    run,
                    operation["manifest"],
                    fixed.limits.__dict__,
                    stage="validation",
                )
        elif kind == "dependency":
            if (
                data["run_id"] != str(run.pk)
                or data["source_digest"] != workspace["source_digest"]
                or data["validation_profile"] != ctx.document["validation_profile"]
            ):
                raise RunConflict(
                    "Dependency setup differs from its owned parent/source."
                )
            profile = contract.profile_from_reference(data["validation_profile"])
            deadline_and_limits(
                run, data, profile.setup_profile().limits.__dict__, stage="validation"
            )
            if not set(data["network_hosts"]) <= set(policy.network_hosts):
                raise RunConflict("Dependency setup requires forbidden network hosts.")
        else:
            publications = [
                entry.payload["data"]
                for entry in all_intents(run)
                if entry.payload["kind"] == "delivery"
            ]
            if any(
                entry["publication_id"] != data["publication_id"]
                for entry in publications
            ):
                raise RunConflict("A native run owns only one publication identity.")
            if any(
                entry["stage"] == data["stage"]
                and entry["workspace_id"] == data["workspace_id"]
                for entry in publications
            ):
                raise RunConflict(
                    "Reconcile the existing publication stage instead of replacing its identity."
                )
            if (
                data["stage"] == "create_pr"
                and run.evidence.filter(
                    kind="native_delivery", payload__pull_request__isnull=False
                )
                .exclude(payload__pull_request=None)
                .exists()
            ):
                raise RunConflict(
                    "A publication revision must preserve the original PR identity."
                )
            if (
                data["stage"] == "create_pr"
                and not run.evidence.filter(
                    kind="native_delivery",
                    payload__publication_id=data["publication_id"],
                    payload__workspace_id=data["workspace_id"],
                    payload__pushed_sha=data["workspace"]["commit_sha"],
                ).exists()
            ):
                raise RunConflict(
                    "Record the owned verified push before creating its pull request."
                )
            if contract.workspace_identity(
                data["workspace"]
            ) != contract.workspace_identity(workspace["workspace"]):
                raise RunConflict(
                    "Native publication differs from its inspected commit."
                )
            check_workspace(run, data["workspace"], ctx, current_namespace=True)
            if (
                workspace["workspace"]["tree_sha"]
                == ctx.document["repository"]["base_tree_sha"]
            ):
                raise RunConflict(
                    "No-change native work cannot publish an empty commit."
                )
            passed = (
                run.evidence.filter(kind="native_validation", payload__state="passed")
                .order_by("-pk")
                .first()
            )
            if (
                passed is None
                or passed.payload["tree_sha"] != workspace["workspace"]["tree_sha"]
                or passed.payload["source_digest"] != workspace["source_digest"]
            ):
                raise RunConflict(
                    "Independent validation must pass before publication."
                )
            from .factory_delivery import validate_factory_workspace

            validate_factory_workspace(run, data["workspace"], ctx)
    existing = {
        operation
        for entry in all_intents(run)
        for operation in contract.intent_operations(entry.payload)
    }
    if set(contract.intent_operations(value)) & existing:
        raise RunConflict("Native operation identity was already registered.")
    require_prior_terminal(run, ready_build=ready_build)


def receipt_binding(run, receipt, manifest, *, passed):
    now = timezone.now().timestamp() + 5
    start, end = receipt["started_at"], receipt["ended_at"]
    if start is not None and (start < run.created_at.timestamp() - 5 or start > now):
        raise RunConflict(
            "Native receipt start does not belong to this run's lifetime."
        )
    if end is not None and end > now:
        raise RunConflict("Native receipt completion is in the future.")
    if passed and (
        not end or end > contract.timestamp(manifest["deadline"]).timestamp() + 5
    ):
        raise RunConflict("Native passing receipt exceeds its original deadline.")


def validate_agent_binding(run, value):
    intent = saved(run, value["intent_id"], "native_intent").payload
    if intent["kind"] != "agent":
        raise RunConflict("Agent result does not reference an agent intent.")
    session = intent["data"]["session"]
    receipt = value["receipt"]
    receipt_binding(run, receipt, session["manifest"], passed=value["successful"])
    if (
        receipt["run_id"] != str(run.pk)
        or receipt["operation_id"] != session["manifest"]["operation_id"]
    ):
        raise RunConflict("Agent receipt belongs to another invocation.")
    if (
        receipt["source_digest"] != session["manifest"]["source_digest"]
        or value["agent_version"] != session["agent_version"]
    ):
        raise RunConflict("Agent result differs from its prepared source/version.")
    identity = session.get("execution_identity")
    if identity is not None and receipt.get("execution_identity") != identity:
        raise RunConflict("Agent result differs from its prepared worker identity.")
    if value["successful"] and not (
        receipt.get("execution_identity") or receipt.get("container_id")
    ):
        raise RunConflict(
            "Agent success requires its original protected execution identity."
        )


def validate_workspace_binding(run, value):
    ctx = context(run, value["context_id"], value["context_digest"])
    intent = saved(run, value["agent_intent_id"], "native_intent").payload
    result = run.evidence.filter(
        kind="native_agent",
        payload__intent_id=value["agent_intent_id"],
        payload__successful=True,
    ).last()
    if (
        intent["kind"] != "agent"
        or intent["attempt"] != value["attempt"]
        or intent["context_id"] != str(ctx.pk)
        or result is None
    ):
        raise RunConflict(
            "Inspect a successful owned agent result before committing its workspace."
        )
    if value["export_digest"] != result.payload["receipt"]["export_digest"]:
        raise RunConflict("Native workspace differs from the verified agent export.")
    check_workspace(run, value["workspace"], ctx)
    if value["attempt"]:
        previous = workspace_fact(run, intent["data"]["workspace_id"], ctx).payload
        if (
            value["workspace"]["base_revision"]
            != previous["workspace"]["base_revision"]
        ):
            raise RunConflict(
                "Agent repair must preserve its preceding inspected base revision."
            )


def validate_validation_binding(run, value):
    intent = saved(run, value["intent_id"], "native_intent").payload
    if intent["kind"] != "validation":
        raise RunConflict("Validation result does not reference a validation intent.")
    data = intent["data"]
    for key in ("run_id", "tree_sha", "source_digest", "profile", "provenance"):
        if value[key] != data[key]:
            raise RunConflict("Validation result differs from its immutable intent.")
    for check, operation in zip(value["checks"], data["operations"]):
        receipt = check["receipt"]
        manifest = operation["manifest"]
        receipt_binding(run, receipt, manifest, passed=value["state"] == "passed")
        if (
            check["id"] != operation["id"]
            or receipt["operation_id"] != manifest["operation_id"]
            or receipt["run_id"] != str(run.pk)
        ):
            raise RunConflict("Validation result belongs to another required command.")
        if (
            value["state"] == "passed"
            and receipt["source_digest"] != data["source_digest"]
        ):
            raise RunConflict("Validation result does not bind the inspected source.")


def validate_delivery_binding(run, value):
    from .merge_services import workspace_entry

    workspace = workspace_entry(run, value["workspace_id"]).payload
    if contract.workspace_identity(
        workspace["workspace"]
    ) != contract.workspace_identity(value["workspace"]):
        raise RunConflict("Published ref differs from its owned workspace.")
    for identity in value["intent_ids"]:
        intent = saved(run, identity, "native_intent").payload
        if (
            intent["kind"] != "delivery"
            or intent["data"]["publication_id"] != value["publication_id"]
            or intent["data"]["workspace_id"] != value["workspace_id"]
        ):
            raise RunConflict("Publication fact does not reference its owned intent.")
        if contract.workspace_identity(
            intent["data"]["workspace"]
        ) != contract.workspace_identity(value["workspace"]):
            raise RunConflict("Publication fact changed the expected commit.")


def validate_termination_binding(run, value):
    from .merge_services import require_merge_terminal

    require_merge_terminal(run)
    intents = {str(entry.operation_id): entry.payload for entry in all_intents(run)}
    if set(value["intent_ids"]) != set(intents):
        raise RunConflict("Termination must cover every registered native intent.")
    expected = {
        identity
        for intent in intents.values()
        for identity in contract.intent_operations(intent)
    }
    actual = {entry["operation_id"]: entry for entry in value["operations"]}
    if set(actual) != expected or any(
        entry["run_id"] != str(run.pk) for entry in actual.values()
    ):
        raise RunConflict("Termination must cover the exact owned executor operations.")
    expected_publications = {
        key for key, intent in intents.items() if intent["kind"] == "delivery"
    }
    observed_publications = {
        entry["intent_id"]: entry for entry in value["publications"]
    }
    if (
        len(observed_publications) != len(value["publications"])
        or set(observed_publications) != expected_publications
    ):
        raise RunConflict("Termination must resolve every publication intent.")
    for identity, entry in observed_publications.items():
        data = intents[identity]["data"]
        if (
            entry["publication_id"] != data["publication_id"]
            or entry["stage"] != data["stage"]
        ):
            raise RunConflict("Publication termination belongs to another intent.")
        if entry["state"] != "not_started":
            fact = run.evidence.filter(
                kind="native_delivery", payload__intent_ids__contains=[identity]
            ).last()
            if (
                fact is None
                or entry["pushed_sha"] != fact.payload["pushed_sha"]
                or entry["pull_request"] != fact.payload["pull_request"]
            ):
                raise RunConflict(
                    "Known publication termination requires its immutable delivery fact."
                )
        elif run.evidence.filter(
            kind="native_delivery", payload__intent_ids__contains=[identity]
        ).exists():
            raise RunConflict("A published ref cannot be reported as unstarted.")
    expected_builds = {
        intent["data"]["build_id"]
        for intent in intents.values()
        if intent["kind"] == "dependency"
    }
    actual_builds = {entry["build_id"]: entry for entry in value["builds"]}
    if (
        len(actual_builds) != len(value["builds"])
        or set(actual_builds) != expected_builds
        or any(entry["run_id"] != str(run.pk) for entry in actual_builds.values())
    ):
        raise RunConflict(
            "Termination must cover owned dependency qualification resources."
        )
    known_receipts = {}
    for entry in run.evidence.filter(kind="native_agent"):
        known_receipts[entry.payload["receipt"]["operation_id"]] = entry.payload[
            "receipt"
        ]["receipt_digest"]
    for entry in run.evidence.filter(kind="native_validation"):
        for check in entry.payload["checks"]:
            known_receipts[check["receipt"]["operation_id"]] = check["receipt"][
                "receipt_digest"
            ]
    if any(
        actual[key]["receipt_digest"] != value for key, value in known_receipts.items()
    ):
        raise RunConflict("Termination changed a protected result already reported.")


def require_termination(run):
    if not registered(run):
        return
    for entry in run.evidence.filter(kind="native_termination").order_by("-pk"):
        try:
            validate_termination_binding(run, entry.payload)
            return
        except RunConflict:
            continue
    raise RunConflict(
        "Confirm every native/local/publication resource terminal before releasing ownership."
    )


@checked
def bind_artifact(run, claim_token, kind, value, operation_id=None):
    contract.validate_native_artifact(kind, value)
    completion = (
        kind == "native_intent"
        and value["kind"] == "merge"
        and value["data"]["stage"] != "merge"
    )
    run, document = lock_scope(
        run, claim_token, reporting=kind != "native_intent" or completion
    )
    if kind == "native_intent":
        from .factory_delivery import validate_delivery_policy
        from .merge_services import completion_authority

        validate_delivery_policy(run, value)
        if completion:
            completion_authority(run, document)
        else:
            authorize(run, document)
        validate_intent_binding(run, value, operation_id)
    elif kind == "native_agent":
        validate_agent_binding(run, value)
    elif kind == "native_workspace":
        validate_workspace_binding(run, value)
    elif kind == "native_validation":
        validate_validation_binding(run, value)
    elif kind == "native_delivery":
        validate_delivery_binding(run, value)
    elif kind == "native_termination":
        validate_termination_binding(run, value)
    elif kind == "native_revision":
        from .merge_services import bind_revision

        bind_revision(run, value)
    elif kind == "native_merge":
        from .merge_services import bind_merge

        bind_merge(run, value)
    elif kind == "native_completion":
        from .merge_services import bind_completion

        bind_completion(run, value)
    elif kind == "native_merge_terminal":
        from .merge_services import bind_merge_terminal

        bind_merge_terminal(run, value)
    return run


@checked
def validate_delivery(run, value):
    from vibearr_runner.native_evidence import validate_native_evidence

    validate_native_evidence(value)
    if (
        value["run_id"] != str(run.pk)
        or value["runner_id"] != run.runner_id
        or value["configuration_revision"] != run.configuration_revision
    ):
        raise RunConflict("Native delivery belongs to another owned configuration/run.")
    if value["policy_digest"] != run.configuration_snapshot["policy_digest"]:
        raise RunConflict("Native delivery changed its frozen policy.")
    policy = native_policy(run)
    if policy.delivery_mode != value["delivery"]["mode"]:
        raise RunConflict("Native delivery mode differs from the frozen policy.")
    ctx = context(run, value["context"]["id"], value["context"]["digest"])
    if str(ctx.pk) != run.context_reference:
        raise RunConflict("Native delivery must use its current context.")
    check_workspace(run, value["workspace"], ctx)
    if (
        value["workspace"]["tree_sha"] == ctx.document["repository"]["base_tree_sha"]
        or value["workspace"]["commit_sha"]
        == ctx.document["repository"]["base_revision"]
    ):
        raise RunConflict("No-change native work cannot claim delivery.")
    workspace = next(
        (
            entry
            for entry in run.evidence.filter(
                kind__in=["native_workspace", "native_revision"]
            ).order_by("-pk")
            if contract.workspace_identity(entry.payload["workspace"])
            == contract.workspace_identity(value["workspace"])
        ),
        None,
    )
    if workspace is None:
        raise RunConflict("Native delivery lacks an inspected committed workspace.")
    agent = run.evidence.filter(
        kind="native_agent",
        payload__intent_id=workspace.payload["agent_intent_id"],
        payload__successful=True,
    ).last()
    if agent is None:
        raise RunConflict("Native delivery lacks its successful native session.")
    session = saved(run, workspace.payload["agent_intent_id"], "native_intent").payload[
        "data"
    ]["session"]
    expected_runtime = {
        "image_digest": session["image_digest"],
        "profile_id": session["manifest"]["profile_id"],
        "agent_version": agent.payload["agent_version"],
        "session_id": agent.payload["session_id"],
    }
    if value["runtime"] != expected_runtime:
        raise RunConflict("Native delivery runtime differs from its protected session.")
    validation = next(
        (
            entry
            for entry in run.evidence.filter(
                kind="native_validation", payload__state="passed"
            ).order_by("-pk")
            if entry.payload["tree_sha"] == value["workspace"]["tree_sha"]
            and entry.payload["source_digest"] == workspace.payload["source_digest"]
        ),
        None,
    )
    if validation is None or value["validation"] != contract.validation_evidence(
        validation.payload
    ):
        raise RunConflict(
            "Native delivery lacks exact required committed-tree validation."
        )
    publication = next(
        (
            entry
            for entry in run.evidence.filter(kind="native_delivery").order_by("-pk")
            if entry.payload["pull_request"] == value["delivery"]["pull_request"]
            and entry.payload["pushed_sha"] == value["delivery"]["pushed_sha"]
            and contract.workspace_identity(entry.payload["workspace"])
            == contract.workspace_identity(value["workspace"])
        ),
        None,
    )
    if publication is None:
        raise RunConflict("Native delivery lacks a verified owned publication.")
    if policy.delivery_mode == "merge":
        from .merge_services import validate_merged_delivery

        validate_merged_delivery(run, value, workspace)
    from .factory_delivery import validate_factory_workspace

    validate_factory_workspace(run, value["workspace"], ctx)
    return value
