from vibearr_runner.delivery import DeliveryError
from vibearr_runner.factory_delivery import (
    factory_enabled,
    require_review_chain,
    specification_criteria,
)
from vibearr_runner.factory_routing import FactoryRoutingError, changed_paths, route
from vibearr_runner.native_contract import digest

from .services import RunConflict


def validate_delivery_policy(run, intent):
    policy = run.configuration_snapshot.get("execution_policy", {})
    if policy.get("delivery_mode") == "merge" or intent.get("kind") == "merge":
        raise RunConflict(
            "Automatic merge is disabled; select pull-request delivery for human review."
        )


def validate_routing(snapshot, records, round_number, context_id, evidence):
    intakes = [
        entry["document"]
        for entry in records
        if entry["document"]["role"] == "intake"
        and entry["document"].get("context_id") == context_id
    ]
    if not intakes or intakes[-1]["payload"].get("verdict") != "accept":
        raise RunConflict("Factory publication requires its saved accepted intake.")
    previous = None
    routes = [
        entry["document"] for entry in records if entry["document"]["role"] == "routing"
    ]
    for document in routes:
        if document["round"] > round_number or document["context_id"] != context_id:
            continue
        attestations = [
            entry["payload"]
            for entry in evidence
            if entry["kind"] == "native_workspace"
            and entry["payload"].get("context_id") == document["context_id"]
            and entry["payload"].get("context_digest") == document["context_digest"]
            and entry["payload"].get("source_digest") == document["source_digest"]
            and entry["payload"].get("workspace", {}).get("tree_sha")
            == document["tree_sha"]
            and entry["payload"].get("workspace", {}).get("base_revision")
            == document["base_sha"]
            and entry["payload"].get("workspace", {}).get("commit_sha")
            == document["head_sha"]
        ]
        if not attestations or "factory_paths" not in attestations[-1]:
            raise RunConflict(
                "Factory routing lacks its attested changed-path inventory."
            )
        try:
            paths = attestations[-1]["factory_paths"]
            if changed_paths(paths) != paths:
                raise FactoryRoutingError("Factory paths must be canonical and sorted.")
            expected = route(
                snapshot["effective"]["factory"],
                intakes[-1]["payload"],
                paths,
                previous=previous,
            )
        except FactoryRoutingError as exc:
            raise RunConflict(str(exc)) from None
        if expected != document["payload"] or expected["protected_paths"]:
            raise RunConflict(
                "Factory routing differs from its trusted configuration and inspected paths."
            )
        previous = expected
    if previous is None:
        raise RunConflict("Factory publication requires its current trusted routing.")


def checked_factory_workspace(
    snapshot, plan, context, workspace, records, evidence, run_id
):
    if not factory_enabled(snapshot):
        return None
    if not plan or not records or len(records) > 1000 or len(evidence) > 2000:
        raise RunConflict(
            "Publication requires its current saved factory specification."
        )
    records = sorted(records, key=lambda entry: entry["sequence"])
    specification_id = plan["document"].get("factory_specification_id")
    specification = next(
        (entry for entry in records if entry["id"] == specification_id), None
    )
    if specification is None:
        raise RunConflict(
            "Publication requires its current saved factory specification."
        )
    for entry in [plan, *records]:
        if (
            entry.get("presentation_only")
            or entry.get("unavailable")
            or digest(entry["document"]) != entry["digest"]
        ):
            raise RunConflict(
                "Factory publication evidence differs from its immutable document digest."
            )
    sources = [
        entry["payload"]
        for entry in evidence
        if entry["kind"] in {"native_workspace", "native_revision"}
        and entry["payload"].get("workspace") == workspace
        and entry["payload"].get("context_id") == context["id"]
        and entry["payload"].get("context_digest") == context["digest"]
    ]
    if not sources:
        raise RunConflict(
            "Factory publication requires its current inspected workspace."
        )
    source = sources[-1]
    binding = {
        "run_id": run_id,
        "context_id": context["id"],
        "context_digest": context["digest"],
        "specification_id": plan["id"],
        "specification_digest": plan["digest"],
        "source_digest": source["source_digest"],
        "tree_sha": workspace["tree_sha"],
        "base_sha": workspace["base_revision"],
        "head_sha": workspace["commit_sha"],
        "configuration_digest": digest(snapshot),
    }
    try:
        criterion_ids = specification_criteria(plan, specification, context["id"])
        result = require_review_chain(records, binding, criterion_ids)
    except DeliveryError as exc:
        raise RunConflict(str(exc)) from None
    validate_routing(snapshot, records, result["round"], context["id"], evidence)
    checks = next(
        entry
        for entry in reversed(records)
        if entry["document"]["role"] == "checks"
        and entry["document"]["round"] == result["round"]
    )
    receipt_id = checks["document"]["payload"]["validation_id"]
    receipts = [
        entry
        for entry in evidence
        if entry["kind"] == "native_validation"
        and entry["payload"].get("source_digest") == binding["source_digest"]
        and entry["payload"].get("tree_sha") == binding["tree_sha"]
    ]
    if (
        not receipts
        or str(receipts[-1]["operation_id"]) != receipt_id
        or any(
            receipts[-1]["payload"].get(key) != value
            for key, value in {
                "state": "passed",
                "source_digest": binding["source_digest"],
                "tree_sha": binding["tree_sha"],
                "run_id": run_id,
            }.items()
        )
    ):
        raise RunConflict(
            "Factory checks lack their current independent validation receipt."
        )
    return result


def validate_factory_workspace(run, workspace, context):
    if not factory_enabled(run.configuration_snapshot):
        return None
    from .plan_models import PlanVersion

    plan = PlanVersion.objects.filter(
        pk=run.plan_reference, run=run, context=context
    ).first()
    if plan is None or not hasattr(run, "factory_artifacts"):
        raise RunConflict(
            "Publication requires its current saved factory specification."
        )
    records = [
        {
            "id": str(entry.pk),
            "sequence": entry.sequence,
            "document": entry.document,
            "digest": entry.digest,
        }
        for entry in run.factory_artifacts.order_by("sequence")[:1001]
    ]
    evidence = list(
        run.evidence.filter(
            kind__in=["native_workspace", "native_revision", "native_validation"]
        )
        .order_by("pk")
        .values("operation_id", "kind", "payload")[:2001]
    )
    return checked_factory_workspace(
        run.configuration_snapshot,
        {"id": str(plan.pk), "digest": plan.digest, "document": plan.document},
        {"id": str(context.pk), "digest": context.digest},
        workspace,
        records,
        evidence,
        str(run.pk),
    )
