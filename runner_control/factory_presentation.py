import json
import re
from collections import defaultdict
from urllib.parse import urlsplit
from uuid import UUID

from django.db.models import OuterRef, Q, Subquery
from django.utils.dateparse import parse_datetime

from vibearr_runner.factory_contract import FactoryContractError, validate_artifact
from vibearr_runner.native_contract import digest

from .factory_delivery import checked_factory_workspace
from .factory_models import FactoryArtifact
from .models import ContextSnapshot, PlanVersion, RunEvidence
from .services import RunConflict

SUMMARY_ARTIFACT_LIMIT = 110
ARTIFACT_PAGE_SIZE = 10
PAYLOAD_DISPLAY_LIMIT = 16000
REVIEWERS = {
    "reviewer-code": "Code review",
    "reviewer-security": "Security review",
    "reviewer-ux": "UX review",
    "reviewer-ui": "UI review",
}
ROLE_LABELS = {
    **REVIEWERS,
    "intake": "Intake",
    "specification": "Specification",
    "build": "Build",
    "checks": "Trusted checks",
    "routing": "Review routing",
    "decision": "Readiness decision",
}


def artifact_data(entry):
    try:
        document = validate_artifact(entry.document)
        fields = (
            "run_id",
            "context_id",
            "specification_id",
            "role",
            "round",
            "operation_id",
        )
        expected = (
            str(entry.run_id),
            str(entry.context_id),
            str(entry.specification_id) if entry.specification_id else None,
            entry.role,
            entry.round,
            str(entry.operation_id),
        )
        if digest(document) != entry.digest or any(
            document[key] != value for key, value in zip(fields, expected)
        ):
            document = None
    except FactoryContractError:
        document = None
    return {
        "id": str(entry.pk),
        "sequence": entry.sequence,
        "role": entry.role,
        "round": entry.round,
        "created_at": entry.created_at.isoformat(),
        "document": document,
        "digest": entry.digest,
    }


def prepare_factory_runs(runs):
    runs = list(runs)
    if not runs:
        return runs
    identities = [run.pk for run in runs]
    latest = FactoryArtifact.objects.filter(
        run_id=OuterRef("run_id"), role=OuterRef("role")
    ).order_by("-sequence")
    route_scope = Q(pk__in=[])
    for run in runs:
        context_ids = references([run], "context_reference")
        if context_ids:
            route_scope |= Q(run_id=run.pk, context_id=context_ids[0])
    routes = FactoryArtifact.objects.filter(
        route_scope,
        role="routing",
        round__range=(1, 100),
    )
    selected = Q(pk__in=Subquery(latest.values("pk")[:1])) | Q(
        pk__in=Subquery(routes.values("pk"))
    )
    entries = FactoryArtifact.objects.filter(
        selected,
        role__in=ROLE_LABELS,
        run_id__in=identities,
    ).order_by("run_id", "sequence")
    grouped = defaultdict(list)
    for entry in entries:
        grouped[entry.run_id].append(artifact_data(entry))
    observations = RunEvidence.objects.filter(
        kind=OuterRef("kind"), run_id=OuterRef("run_id")
    ).order_by("-pk")
    attestation = RunEvidence.objects.filter(
        run_id=OuterRef("run_id"),
        kind="native_workspace",
        payload__source_digest=OuterRef("document__source_digest"),
        payload__workspace__tree_sha=OuterRef("document__tree_sha"),
        payload__workspace__base_revision=OuterRef("document__base_sha"),
        payload__workspace__commit_sha=OuterRef("document__head_sha"),
    ).order_by("-pk")
    proofs = routes.annotate(
        attestation_id=Subquery(attestation.values("pk")[:1])
    ).values("attestation_id")
    validation = RunEvidence.objects.filter(
        run_id=OuterRef("run_id"),
        kind="native_validation",
        payload__source_digest=OuterRef("document__source_digest"),
        payload__tree_sha=OuterRef("document__tree_sha"),
    ).order_by("-pk")
    checks = FactoryArtifact.objects.filter(
        run_id__in=identities, role="checks", pk__in=Subquery(latest.values("pk")[:1])
    )
    receipts = checks.annotate(receipt_id=Subquery(validation.values("pk")[:1])).values(
        "receipt_id"
    )
    selected_observations = RunEvidence.objects.filter(
        Q(pk__in=Subquery(observations.values("pk")[:1]))
        | Q(pk__in=Subquery(proofs))
        | Q(pk__in=Subquery(receipts)),
        kind__in=[
            "publication_observation",
            "verified_delivery",
            "native_workspace",
            "native_validation",
        ],
        run_id__in=identities,
    ).order_by("pk")
    publication = defaultdict(dict)
    evidence = defaultdict(list)
    for entry in selected_observations:
        payload = entry.payload if isinstance(entry.payload, dict) else {}
        if entry.kind in {"publication_observation", "verified_delivery"}:
            publication[entry.run_id][entry.kind] = payload
        if entry.kind in {"native_workspace"}:
            publication[entry.run_id]["candidate"] = payload
        if entry.kind in {"native_workspace", "native_validation"}:
            evidence[entry.run_id].append(
                {
                    "operation_id": str(entry.operation_id),
                    "kind": entry.kind,
                    "payload": payload,
                }
            )
    contexts = {
        str(entry.pk): entry
        for entry in ContextSnapshot.objects.filter(
            pk__in=references(runs, "context_reference"), run_id__in=identities
        )
    }
    plans = {
        str(entry.pk): entry
        for entry in PlanVersion.objects.filter(
            pk__in=references(runs, "plan_reference"), run_id__in=identities
        )
    }
    for run in runs:
        run._factory_documents = grouped[run.pk]
        run._publication_observation = publication[run.pk]
        context = contexts.get(run.context_reference)
        plan = plans.get(run.plan_reference)
        run._factory_authority = authority_data(
            run, context, plan, publication[run.pk], evidence[run.pk]
        )
    return runs


def references(runs, field):
    values = []
    for run in runs:
        try:
            values.append(UUID(getattr(run, field)))
        except (ValueError, TypeError, AttributeError):
            continue
    return values


def authority_data(run, context, plan, publication, evidence):
    if (
        context is None
        or plan is None
        or context.run_id != run.pk
        or plan.run_id != run.pk
        or plan.context_id != context.pk
    ):
        return {}
    source = publication.get("candidate", {})
    candidate = source.get("workspace", {})
    if not isinstance(candidate, dict):
        return {}
    return {
        "binding": {
            "run_id": str(run.pk),
            "context_id": str(context.pk),
            "context_digest": context.digest,
            "specification_id": str(plan.pk),
            "specification_digest": plan.digest,
            "source_digest": source.get("source_digest"),
            "tree_sha": candidate.get("tree_sha"),
            "base_sha": candidate.get("base_revision"),
            "head_sha": candidate.get("commit_sha"),
            "configuration_digest": digest(run.configuration_snapshot),
        },
        "snapshot": run.configuration_snapshot,
        "context": {"id": str(context.pk), "digest": context.digest},
        "plan": {"id": str(plan.pk), "digest": plan.digest, "document": plan.document},
        "workspace": candidate,
        "evidence": evidence,
    }


def binding(document):
    return tuple(
        document.get(key)
        for key in (
            "run_id",
            "context_id",
            "context_digest",
            "specification_id",
            "specification_digest",
            "source_digest",
            "tree_sha",
            "base_sha",
            "head_sha",
            "configuration_digest",
            "round",
        )
    )


def present_artifact(data, current_binding=None, *, specification_id="", intake_id=""):
    result = {
        "id": data["id"],
        "sequence": data["sequence"],
        "role": data["role"],
        "role_label": ROLE_LABELS.get(data["role"], "Unknown role"),
        "round": data["round"],
        "created_at": data["created_at"],
        "digest": data["digest"],
        "unavailable": True,
    }
    try:
        document = validate_artifact(data["document"])
    except FactoryContractError:
        return result
    payload = document["payload"]
    execution = document.get("execution", {})
    findings = payload.get("findings", []) if data["role"] in REVIEWERS else []
    encoded = json.dumps(payload, sort_keys=True, indent=2, ensure_ascii=False)
    criteria = []
    items = payload.get("criteria", [])
    for index, criterion in enumerate(items if isinstance(items, list) else []):
        if isinstance(criterion, str):
            criteria.append({"id": f"criterion-{index + 1}", "text": criterion})
        elif isinstance(criterion, dict):
            criteria.append(
                {
                    "id": criterion.get("id"),
                    "text": criterion.get("description", criterion.get("text")),
                }
            )
    result.update(
        unavailable=False,
        document=document,
        payload=payload,
        criteria=criteria,
        findings=findings[:20],
        more_findings=len(findings) > 20,
        payload_json=encoded[:PAYLOAD_DISPLAY_LIMIT],
        payload_truncated=len(encoded) > PAYLOAD_DISPLAY_LIMIT,
        current=(current_binding is not None and binding(document) == current_binding)
        or data["id"] in {specification_id, intake_id},
        current_input=data["id"] in {specification_id, intake_id},
        execution=execution,
        elapsed=(
            f'{execution["ended_at"] - execution["started_at"]:.2f} seconds'
            if execution
            else "Not reported"
        ),
    )
    return result


def factory_summary(data, authority=None):
    entries = [present_artifact(entry) for entry in data]
    valid = [entry for entry in entries if not entry["unavailable"]]
    if not valid:
        return {
            "present": bool(data),
            "unavailable": bool(data),
            "reviewers": [],
            "ready": False,
        }
    authority = authority or {}
    current_authority = authority.get("binding", {})
    last_context = current_authority.get(
        "context_id", valid[-1]["document"]["context_id"]
    )
    same_context = [
        entry for entry in valid if entry["document"]["context_id"] == last_context
    ]
    builds = [entry for entry in same_context if entry["role"] == "build"]
    current = builds[-1] if builds else valid[-1]
    current_document = {**current["document"], **current_authority}
    current_binding = binding(current_document)
    matching = {
        entry["role"]: entry
        for entry in same_context
        if current_authority and binding(entry["document"]) == current_binding
    }
    routing = matching.get("routing")
    required = (
        routing["payload"]["required_reviewers"] if routing else ["reviewer-code"]
    )
    reviewers = []
    for role in required:
        entry = matching.get(role)
        verdict = entry["payload"]["verdict"] if entry else "missing"
        reviewers.append(
            {
                "role": role,
                "label": REVIEWERS[role],
                "verdict": verdict,
                "artifact_id": entry["id"] if entry else "",
            }
        )
    checks = matching.get("checks")
    decision = matching.get("decision")
    ready = False
    specification_id = (
        authority.get("plan", {}).get("document", {}).get("factory_specification_id")
    )
    specs = [
        entry
        for entry in same_context
        if entry["role"] == "specification"
        and (not specification_id or entry["id"] == specification_id)
    ]
    try:
        if any(entry["unavailable"] for entry in entries):
            raise RunConflict("Evidence unavailable")
        ready = (
            checked_factory_workspace(
                authority["snapshot"],
                authority["plan"],
                authority["context"],
                authority["workspace"],
                data,
                authority["evidence"],
                current_authority["run_id"],
            )
            is not None
        )
    except (RunConflict, KeyError, TypeError, ValueError, AttributeError):
        pass
    intake = [entry for entry in same_context if entry["role"] == "intake"]
    scope = (
        specs[-1]["payload"]["scope"]
        if specs
        else (intake[-1]["payload"]["scope"] if intake else "Not reported")
    )
    risk = (
        routing["payload"]["risk"]
        if routing
        else (intake[-1]["payload"]["risk"] if intake else "Not reported")
    )
    surfaces = routing["payload"]["surfaces"] if routing else []
    return {
        "present": True,
        "unavailable": False,
        "round": current["round"],
        "binding": current_binding if current_authority else None,
        "tree_sha": current_document["tree_sha"],
        "specification_id": current_document["specification_id"],
        "specification_digest": current_document["specification_digest"],
        "specification_artifact_id": specification_id if specs else "",
        "intake_artifact_id": intake[-1]["id"] if current_authority and intake else "",
        "scope": scope,
        "risk": risk,
        "surfaces": surfaces,
        "routing_reported": routing is not None,
        "reviewers": reviewers,
        "checks": checks["payload"]["state"] if checks else "Not reported",
        "decision": decision["payload"]["verdict"] if decision else "Not reported",
        "ready": ready,
    }


def publication_summary(payload, result_url, *, state="", outcome="", cursor=None):
    unknown = {"status": "unknown", "label": "Merge not yet observed"}
    if isinstance(cursor, dict) and cursor.get("status") == "blocked":
        unknown["label"] = "Observation unavailable; merge not verified"
    if state != "completed" or outcome != "delivered":
        return unknown
    if not isinstance(payload, dict):
        return unknown
    try:
        observation = payload["publication_observation"]
        delivery = payload["verified_delivery"]
        workspace = delivery["workspace"]
        pull = delivery["delivery"]["pull_request"]
        observed = parse_datetime(observation.get("observed_at", ""))
        number = int(urlsplit(result_url).path.rstrip("/").split("/")[-1])
    except (ValueError, TypeError, AttributeError, KeyError):
        return unknown
    if not all(
        isinstance(value, dict) for value in [observation, delivery, workspace, pull]
    ):
        return unknown
    if (
        observation.get("schema_version") != 1
        or delivery.get("execution_kind") != "native"
    ):
        return unknown
    if (
        delivery["delivery"].get("mode") != "pull_request"
        or pull.get("url") != result_url
    ):
        return unknown
    expected = {
        "repository_id": workspace.get("repository_id"),
        "pull_number": pull.get("number"),
        "head_sha": workspace.get("commit_sha"),
        "base_sha": workspace.get("base_revision"),
    }
    if (
        observed is None
        or number != pull.get("number")
        or any(
            observation.get(key) != value or not value
            for key, value in expected.items()
        )
    ):
        return unknown
    status = observation.get("status")
    labels = {
        "awaiting_review": "Awaiting human review",
        "closed_unmerged": "Closed without merge",
        "merged": "Human merge verified",
    }
    if status not in labels:
        return unknown
    if status == "merged":
        actor = observation.get("merged_by")
        proof = observation.get("ancestry_proof")
        if (
            not isinstance(actor, dict)
            or actor.get("type") != "User"
            or type(actor.get("id")) is not int
            or actor["id"] < 1
            or not isinstance(actor.get("login"), str)
            or not actor["login"]
        ):
            return unknown
        if (
            not isinstance(observation.get("merge_sha"), str)
            or not re.fullmatch(r"[a-f0-9]{40}|[a-f0-9]{64}", observation["merge_sha"])
            or not isinstance(observation.get("merged_at"), str)
            or parse_datetime(observation["merged_at"]) is None
            or not isinstance(proof, dict)
            or not proof
        ):
            return unknown
        publication_base = proof.get("publication_base", {})
        current_base = proof.get("current_base", {})
        if not isinstance(publication_base, dict) or not isinstance(current_base, dict):
            return unknown
        if (
            publication_base.get("base_sha") != workspace["base_revision"]
            or publication_base.get("head_sha") != observation["merge_sha"]
        ):
            return unknown
        if current_base.get("base_sha") != observation[
            "merge_sha"
        ] or not current_base.get("head_sha"):
            return unknown
        if any(
            entry.get("status") not in {"ahead", "identical"}
            for entry in [publication_base, current_base]
        ):
            return unknown
    return {
        "status": status,
        "label": labels[status],
        "observed_at": observed,
        "merge_sha": observation.get("merge_sha", ""),
    }
