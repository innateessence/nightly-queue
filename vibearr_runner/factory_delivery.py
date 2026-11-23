"""Current immutable review chains required before publication."""

from .delivery import DeliveryError
from .factory_generation import PublicationBaseChanged as PublicationBaseChanged
from .native_contract import digest


def factory_enabled(snapshot):
    return snapshot.get("effective", {}).get("factory", {}).get("enabled") is True


def require_human_delivery(policy):
    if policy.delivery_mode != "pull_request":
        raise DeliveryError(
            "Automatic merge is disabled; a human must merge the pull request."
        )


def specification_criteria(plan, artifact, context_id):
    document = artifact.get("document") or {}
    if (
        artifact.get("id") != plan["document"].get("factory_specification_id")
        or artifact.get("digest")
        != plan["document"].get("factory_specification_digest")
        or artifact.get("presentation_only")
        or artifact.get("unavailable")
        or document.get("role") != "specification"
        or document.get("context_id") != context_id
    ):
        raise DeliveryError(
            "Factory delivery requires the plan's exact saved specification artifact."
        )
    values = document.get("payload", {}).get("criteria")
    if not isinstance(values, list) or not values or len(values) > 100:
        raise DeliveryError("Factory specification criteria are unavailable.")
    identities = [
        (
            value.get("id", f"criterion-{index + 1}")
            if isinstance(value, dict)
            else f"criterion-{index + 1}"
        )
        for index, value in enumerate(values)
    ]
    if len(set(identities)) != len(identities) or any(
        not isinstance(value, str) or not value for value in identities
    ):
        raise DeliveryError("Factory specification criterion identities are invalid.")
    return identities


def require_review_chain(records, binding, criterion_ids=()):
    try:
        return checked_review_chain(records, binding, criterion_ids)
    except (KeyError, TypeError, ValueError, AttributeError):
        raise DeliveryError(
            "Publication requires a complete current factory review chain."
        ) from None


def verified_detail(detail, summary=None):
    if (
        not isinstance(detail, dict)
        or detail.get("presentation_only")
        or detail.get("unavailable")
    ):
        raise DeliveryError("Factory review evidence is unavailable.")
    document = detail.get("document")
    if not isinstance(document, dict) or detail.get("digest") != digest(document):
        raise DeliveryError(
            "Factory review document digest differs from its saved identity."
        )
    if summary is not None:
        fields = (
            "id",
            "sequence",
            "digest",
            "role",
            "round",
            "context_id",
            "specification_id",
        )
        if any(detail.get(key) != summary.get(key) for key in fields):
            raise DeliveryError(
                "Factory review summary differs from its immutable detail."
            )
        if any(document.get(key) != detail.get(key) for key in fields[3:]):
            raise DeliveryError("Factory review metadata differs from its document.")
    return detail


def artifact_summaries(host):
    records, after = [], 0
    for _ in range(101):
        page = host.client.request(
            "GET", f"/api/v1/runner/runs/{host.run_id}/factory/artifacts/?after={after}"
        )
        entries = page.get("artifacts")
        if not isinstance(entries, list) or len(entries) > 10:
            raise DeliveryError("Factory artifact pagination is invalid.")
        sequences = [entry.get("sequence") for entry in entries]
        if any(type(value) is not int or value <= after for value in sequences):
            raise DeliveryError("Factory artifact pagination is inconsistent.")
        if sequences != sorted(set(sequences)):
            raise DeliveryError(
                "Factory artifact pagination repeats or reorders evidence."
            )
        records.extend(entries)
        if len(records) > 1000:
            raise DeliveryError(
                "Factory artifact history exceeds its bounded read limit."
            )
        if page.get("has_more") is False:
            return records
        if (
            not entries
            or page.get("has_more") is not True
            or page.get("next_after") != sequences[-1]
        ):
            raise DeliveryError("Factory artifact pagination is incomplete.")
        after = sequences[-1]
    raise DeliveryError("Factory artifact pagination did not terminate.")


def checked_review_chain(records, binding, criterion_ids):
    if not isinstance(records, list) or not records or len(records) > 1000:
        raise ValueError
    sequences = [entry["sequence"] for entry in records]
    if any(type(value) is not int or value < 1 for value in sequences):
        raise ValueError
    if len(set(sequences)) != len(sequences):
        raise ValueError
    records = sorted(records, key=lambda entry: entry["sequence"])
    builds = [entry for entry in records if entry["document"]["role"] == "build"]
    if not builds:
        raise ValueError
    build = builds[-1]
    document = build["document"]
    round_number = document["round"]
    if type(round_number) is not int or round_number < 1:
        raise ValueError
    current = {}
    for entry in records:
        value = entry["document"]
        if value["round"] == round_number:
            current[value["role"]] = entry
    selected = [build]
    for role in ("checks", "routing", "decision"):
        selected.append(current[role])
    checks, routing, decision = selected[1:]
    if checks["sequence"] >= routing["sequence"]:
        raise ValueError
    required = routing["document"]["payload"]["required_reviewers"]
    allowed = {"reviewer-code", "reviewer-security", "reviewer-ux", "reviewer-ui"}
    if (
        not isinstance(required, list)
        or "reviewer-code" not in required
        or len(set(required)) != len(required)
        or not set(required) <= allowed
    ):
        raise ValueError
    surfaces = set(routing["document"]["payload"].get("surfaces", []))
    if "security" in surfaces and "reviewer-security" not in required:
        raise ValueError
    if "ui" in surfaces and not {"reviewer-ux", "reviewer-ui"} <= set(required):
        raise ValueError
    if checks["document"]["payload"]["state"] != "passed":
        raise ValueError
    mapping = build["document"]["payload"]["criteria"]
    if set(mapping) != set(criterion_ids) or len(mapping) != len(set(mapping)):
        raise ValueError
    for role in required:
        reviewer = current[role]
        payload = reviewer["document"]["payload"]
        if (
            set(payload) != {"verdict", "findings", "coverage"}
            or payload["verdict"] != "pass"
            or payload["findings"]
        ):
            raise ValueError
        coverage = payload["coverage"]
        if set(criterion_ids) != set(coverage) or len(coverage) != len(set(coverage)):
            raise ValueError
        if reviewer["sequence"] <= routing["sequence"]:
            raise ValueError
        selected.append(reviewer)
    for entry in selected:
        value = entry["document"]
        if any(value.get(key) != expected for key, expected in binding.items()):
            raise ValueError
        if entry is not build and entry["sequence"] <= build["sequence"]:
            raise ValueError
    agents = [build, decision] + [current[role] for role in required]
    for field in ("session_id", "operation_id", "intent_id", "result_id"):
        identities = [entry["document"]["execution"][field] for entry in agents]
        if any(not value for value in identities) or len(set(identities)) != len(
            identities
        ):
            raise ValueError
    decision_payload = decision["document"]["payload"]
    required_ids = {
        entry["id"]
        for entry in selected
        if entry is not build and entry is not decision
    }
    if (
        set(decision_payload) != {"verdict", "rationale", "artifact_ids"}
        or decision_payload["verdict"] != "ready"
        or set(decision_payload["artifact_ids"]) != required_ids
        or len(decision_payload["artifact_ids"]) != len(required_ids)
        or any(
            entry["sequence"] >= decision["sequence"]
            for entry in selected
            if entry is not decision
        )
    ):
        raise ValueError
    return {
        "decision_id": decision["id"],
        "round": round_number,
        "required_reviewers": required,
    }


def factory_delivery_barrier(host):
    require_human_delivery(host.policy)
    snapshot = getattr(host, "snapshot", {})
    if not factory_enabled(snapshot):
        return None
    context = host.native["context"]
    workspace = host.native["candidate"]["workspace"]
    run = host.client.request("GET", f"/api/v1/runner/runs/{host.run_id}/")["run"]
    plan_id = run["plan_reference"]
    plan = host.client.request(
        "GET", f"/api/v1/runner/runs/{host.run_id}/plans/{plan_id}/"
    )
    plan = verified_detail(plan.get("plan", plan))
    if plan.get("id") != plan_id:
        raise DeliveryError(
            "Factory publication plan differs from the current saved plan."
        )
    specification_id = plan["document"].get("factory_specification_id")
    if not specification_id or plan.get("presentation_only") or plan.get("unavailable"):
        raise DeliveryError("Factory publication specification is unavailable.")
    specification = verified_detail(
        host.client.request(
            "GET",
            f"/api/v1/runner/runs/{host.run_id}/factory/artifacts/{specification_id}/",
        )["artifact"]
    )
    criterion_ids = specification_criteria(plan, specification, context["id"])
    summaries = artifact_summaries(host)
    builds = [entry for entry in summaries if entry.get("role") == "build"]
    if not builds:
        raise DeliveryError("Factory publication has no saved build artifact.")
    round_number = builds[-1]["round"]
    selected = {}
    for entry in summaries:
        if entry.get("round") == round_number:
            selected[entry["role"]] = entry
    records = []
    for entry in selected.values():
        detail = host.client.request(
            "GET", f"/api/v1/runner/runs/{host.run_id}/factory/artifacts/{entry['id']}/"
        )["artifact"]
        records.append(verified_detail(detail, entry))
    binding = {
        "run_id": host.run_id,
        "context_id": context["id"],
        "context_digest": context["digest"],
        "specification_id": plan_id,
        "specification_digest": plan["digest"],
        "source_digest": host.native["candidate"]["source_digest"],
        "tree_sha": workspace["tree_sha"],
        "base_sha": workspace["base_revision"],
        "head_sha": workspace["commit_sha"],
        "configuration_digest": digest(snapshot),
    }
    return require_review_chain(records, binding, criterion_ids)
