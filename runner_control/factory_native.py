"""Owned factory dispatch authority extends, rather than replaces, native receipts."""

from .services import RunConflict
from vibearr_runner import native_contract as contract
from vibearr_runner.factory_contract import validate_role_intent
from vibearr_runner.factory_roles import (
    retain_criteria,
    retain_requested_criteria,
    validate_context,
)
from vibearr_runner.native_sessions import NativeSession


def settings(run):
    selected = run.configuration_snapshot["effective"].get("factory", {})
    if selected.get("enabled") is not True or selected.get("schema_version") != 1:
        raise RunConflict("Factory execution is not enabled in the frozen settings.")
    return selected


def context_binding(run, document):
    configuration = settings(run)
    if "factory" not in document:
        raise RunConflict("Factory execution requires frozen role registrations.")
    validate_context(
        document["factory"], document["runtime_binding_digest"], configuration
    )


def latest_artifact(run, ctx, role, *, round=None):
    artifacts = run.factory_artifacts.filter(context=ctx, role=role)
    if round is not None:
        artifacts = artifacts.filter(round=round)
    value = artifacts.order_by("sequence").last()
    if value is None:
        raise RunConflict(
            "Factory dispatch requires its prior immutable " + role + " artifact."
        )
    return value


def candidate_binding(run, plan, workspace):
    from .native_services import saved

    if "factory_paths" not in workspace:
        raise RunConflict(
            "Factory candidate requires its trusted changed-path inventory."
        )
    contract.validate_factory_paths(workspace["factory_paths"])
    intent = saved(run, workspace["agent_intent_id"], "native_intent").payload
    descriptor = intent["data"].get("factory", {})
    expected = {
        "specification_id": str(plan.pk),
        "specification_digest": plan.digest,
        "source_digest": workspace["source_digest"],
        "tree_sha": workspace["workspace"]["tree_sha"],
        "base_sha": workspace["workspace"]["base_revision"],
        "head_sha": workspace["workspace"]["commit_sha"],
    }
    if (
        descriptor.get("role") != "build"
        or descriptor.get("round") != workspace["attempt"] + 1
        or any(
            descriptor.get(key) != expected[key]
            for key in ("specification_id", "specification_digest")
        )
    ):
        raise RunConflict(
            "Factory candidate differs from its original builder specification."
        )
    return expected


def require_artifact_binding(entries, expected):
    if any(
        any(entry.document.get(key) != value for key, value in expected.items())
        for entry in entries
    ):
        raise RunConflict(
            "Factory prerequisite belongs to another specification or candidate."
        )


def repair_trigger(run, ctx, expected, round):
    checks = latest_artifact(run, ctx, "checks", round=round)
    entries = [checks]
    if checks.document["payload"]["state"] == "passed":
        routing = latest_artifact(run, ctx, "routing", round=round)
        reviews = [
            latest_artifact(run, ctx, role, round=round)
            for role in routing.document["payload"]["required_reviewers"]
        ]
        entries.extend([routing, *reviews])
        verdicts = [entry.document["payload"]["verdict"] for entry in reviews]
        if "changes" not in verdicts or not set(verdicts) <= {"pass", "changes"}:
            raise RunConflict(
                "Factory repair requires completed reviews requesting changes."
            )
    elif checks.document["payload"]["state"] != "failed":
        raise RunConflict(
            "Factory repair requires actual failed checks or requested changes."
        )
    require_artifact_binding(entries, expected)


def plan_binding(run, document, ctx):
    settings(run)
    specification = latest_artifact(run, ctx, "specification")
    intake = latest_artifact(run, ctx, "intake")
    payload = specification.document["payload"]
    if intake.document["payload"].get("verdict") != "accept":
        raise RunConflict(
            "Only accepted intake can produce an implementation specification."
        )
    retain_requested_criteria(ctx.document, intake.document["payload"])
    retain_criteria(intake.document["payload"], payload)
    expected = [
        value if isinstance(value, str) else value.get("description", value.get("text"))
        for value in payload["criteria"]
    ]
    if (
        document["acceptance_criteria"] != expected
        or document.get("factory_specification_id") != str(specification.pk)
        or document.get("factory_specification_digest") != specification.digest
        or document["approach"]
        != payload["scope"] + "\n\n" + "\n".join(payload["verification"])
        or document["problem"] != ctx.document["source"]["title"]
    ):
        raise RunConflict(
            "Native factory plan differs from its actual saved specification."
        )


def dispatch_binding(run, value, ctx, policy):
    from .native_services import (
        all_intents,
        deadline_and_limits,
        selected_account,
        workspace_fact,
    )
    from vibearr_runner.native_conformance import validate_role_binding

    configuration = settings(run)
    descriptor = validate_role_intent(value["data"]["factory"])
    if "specification_id" not in descriptor:
        raise RunConflict(
            "New factory dispatch requires its frozen specification binding."
        )
    role, round = descriptor["role"], descriptor["round"]
    if round != value["attempt"] + 1:
        raise RunConflict("Factory round differs from its shared repair attempt.")
    context_binding(run, ctx.document)
    selected = configuration.get("roles", {}).get(role)
    if not isinstance(selected, dict) or not selected.get("model"):
        raise RunConflict("Factory role has no frozen model selection.")
    session = NativeSession.from_data(value["data"]["session"])
    account = {**selected_account(run), "model": selected["model"]}
    entry = ctx.document["factory"]["profiles"].get(role)
    if entry is None:
        raise RunConflict("Factory role was not registered in this context.")
    validate_role_binding(
        entry,
        role,
        runtime_binding_digest=ctx.document["runtime_binding_digest"],
        execution_identity=session.execution_identity,
    )
    if (
        session.account != account
        or session.adapter != account["backend"]
        or session.manifest["run_id"] != str(run.pk)
        or session.runtime_binding_digest != ctx.document["runtime_binding_digest"]
        or session.manifest["profile_id"] != entry["profile_id"]
        or session.manifest["profile_digest"] != entry["profile_digest"]
        or session.image_digest != entry["qualification"]["image_digest"]
        or descriptor["input_digest"] != session.manifest["context_digest"]
    ):
        raise RunConflict(
            "Factory session differs from its frozen role/account/worker binding."
        )
    reference = next(
        item
        for item in run.configuration_snapshot["credential_references"]
        if item["id"] == account["credential_id"]
    )
    if session.auth_kind != reference["kind"] or not set(
        session.manifest["network_hosts"]
    ) <= set(policy.network_hosts):
        raise RunConflict("Factory role authentication or network differs from policy.")
    for previous in all_intents(run):
        data = previous.payload.get("data", {})
        frozen = data.get("factory")
        if (
            previous.payload["context_id"] == str(ctx.pk)
            and frozen
            and frozen["role"] == role
            and frozen["round"] == round
        ):
            raise RunConflict(
                "Reconcile the original factory role operation; do not replace it."
            )
    expected_source = ctx.document["source_digest"]
    if role in {"intake", "specification"}:
        if (
            descriptor["specification_id"] is not None
            or descriptor["specification_digest"]
        ):
            raise RunConflict(
                "Intake/specification bootstrap must not claim an existing specification."
            )
        if value["attempt"] != 0 or value["data"]["workspace_id"] is not None:
            raise RunConflict(
                "Intake/specification inspect only the frozen initial source."
            )
        if role == "specification":
            intake = latest_artifact(run, ctx, "intake", round=1)
            if intake.document["payload"].get("verdict") != "accept":
                raise RunConflict("Specification requires accepted intake.")
    else:
        plan = (
            run.plans.filter(pk=run.plan_reference, context=ctx).first()
            if run.plan_reference
            else None
        )
        if plan is None:
            raise RunConflict(
                "Save the actual factory specification before building or reviewing."
            )
        if (
            descriptor["specification_id"] != str(plan.pk)
            or descriptor["specification_digest"] != plan.digest
        ):
            raise RunConflict(
                "Factory dispatch differs from the current specification."
            )
        plan_binding(run, plan.document, ctx)
        workspace_id = value["data"]["workspace_id"]
        if role == "build" and value["attempt"] == 0:
            if workspace_id is not None:
                raise RunConflict(
                    "Initial factory build starts from the inspected base."
                )
        else:
            workspace = workspace_fact(run, workspace_id, ctx).payload
            expected_attempt = (
                value["attempt"] - 1 if role == "build" else value["attempt"]
            )
            if workspace["attempt"] != expected_attempt:
                raise RunConflict("Factory role source belongs to a different round.")
            expected_source = workspace["source_digest"]
            expected_candidate = candidate_binding(run, plan, workspace)
            if role == "build":
                repair_trigger(run, ctx, expected_candidate, round - 1)
        if role.startswith("reviewer-") or role == "decision":
            routing = latest_artifact(run, ctx, "routing", round=round)
            checks = latest_artifact(run, ctx, "checks", round=round)
            if checks.document["payload"].get("state") != "passed":
                raise RunConflict("Review requires actual passing independent checks.")
            require_artifact_binding([routing, checks], expected_candidate)
            required = routing.document["payload"]["required_reviewers"]
            if role.startswith("reviewer-") and role not in required:
                raise RunConflict("Factory reviewer is not in the trusted routed set.")
            if role == "decision":
                for reviewer in required:
                    artifact = latest_artifact(run, ctx, reviewer, round=round)
                    require_artifact_binding([artifact], expected_candidate)
                    if artifact.document["payload"].get("verdict") != "pass":
                        raise RunConflict(
                            "Decision requires every current required review to pass."
                        )
    if session.manifest["source_digest"] != expected_source:
        raise RunConflict("Factory role source differs from its inspected candidate.")
    ceiling = {
        "cpu": policy.cpu_limit,
        "memory_mb": policy.memory_mb,
        "pids": policy.pids_limit,
        "workspace_mb": policy.workspace_mb,
    }
    deadline_and_limits(run, session.manifest, ceiling, stage="implementation")


def terminal_outcome(run, outcome):
    if outcome not in {"needs_info", "rejected"}:
        return
    settings(run)
    expected = "needs_info" if outcome == "needs_info" else "reject"
    intake = (
        run.factory_artifacts.filter(role="intake", context_id=run.context_reference)
        .order_by("sequence")
        .last()
    )
    if intake is None or intake.document["payload"].get("verdict") != expected:
        raise RunConflict("Factory terminal reason requires its actual intake verdict.")
    if run.evidence.filter(kind="native_delivery").exists():
        raise RunConflict("Delivered work cannot be reported as unresolved intake.")
