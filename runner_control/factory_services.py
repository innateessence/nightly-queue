from django.core.exceptions import ValidationError
from django.db import transaction

from credentials.redaction import redact_run_payload
from vibearr_runner import factory_contract as contract
from vibearr_runner import native_contract as native

from .factory_models import FactoryArtifact
from .plan_models import ContextSnapshot, PlanVersion
from .plan_services import locked_run, previous_operation, require_safe_document
from .services import RunConflict, redact_payload


def checked_document(document):
    try:
        return contract.validate_artifact(document)
    except contract.FactoryContractError as exc:
        raise ValidationError({"document": str(exc)}) from None


def evidence(run, operation_id, kind):
    entry = run.evidence.filter(operation_id=operation_id, kind=kind).first()
    if entry is None:
        raise RunConflict("Factory artifact requires its saved native evidence.")
    try:
        value = entry.payload
        if kind == "native_intent" and "factory" in value.get("data", {}):
            contract.validate_role_intent(value["data"]["factory"])
            value = {
                **value,
                "data": {
                    key: item for key, item in value["data"].items() if key != "factory"
                },
            }
        native.validate_native_artifact(kind, value)
    except (native.NativeContractError, contract.FactoryContractError) as exc:
        raise RunConflict(str(exc)) from None
    return entry.payload


def bind_dispatched_specification(intent, document):
    descriptor = intent["data"].get("factory")
    if not isinstance(descriptor, dict) or any(
        key not in descriptor or descriptor[key] != document[key]
        for key in ("specification_id", "specification_digest")
    ):
        raise RunConflict(
            "Factory artifact differs from its dispatch-time specification."
        )


def bind_source(run, document, context):
    if document["role"] in {"intake", "specification"}:
        repository = context.document["repository"]
        expected = (
            context.document["source_digest"],
            repository["base_tree_sha"],
            repository["base_revision"],
            "",
        )
        workspace = None
    else:
        entry = run.evidence.filter(kind="native_workspace").order_by("-pk").first()
        if entry is None:
            raise RunConflict(
                "Factory candidate requires a saved owned native workspace."
            )
        workspace = evidence(run, entry.operation_id, "native_workspace")
        source = workspace.get("workspace", {})
        actual = (
            workspace.get("source_digest"),
            source.get("tree_sha"),
            source.get("base_revision"),
            source.get("commit_sha"),
        )
        expected = tuple(
            document[key]
            for key in ("source_digest", "tree_sha", "base_sha", "head_sha")
        )
        if workspace.get("context_id") != str(context.pk) or actual != expected:
            raise RunConflict(
                "Factory candidate differs from the current owned native workspace."
            )
        intent = evidence(run, workspace["agent_intent_id"], "native_intent")
        bind_dispatched_specification(intent, document)
        return workspace
    actual = tuple(
        document[key] for key in ("source_digest", "tree_sha", "base_sha", "head_sha")
    )
    if actual != expected:
        raise RunConflict("Factory source differs from its inspected context/base.")
    return workspace


def bind_execution(run, document, context, workspace):
    execution = document["execution"]
    intent = evidence(run, execution["intent_id"], "native_intent")
    result = evidence(run, execution["result_id"], "native_agent")
    if (
        intent["kind"] != "agent"
        or intent["context_id"] != str(context.pk)
        or intent["context_digest"] != context.digest
    ):
        raise RunConflict(
            "Factory role requires an owned agent intent for its context."
        )
    if (
        result["intent_id"] != execution["intent_id"]
        or result["successful"] is not True
    ):
        raise RunConflict("Factory role requires its successful native result.")
    descriptor = intent["data"].get("factory")
    expected_descriptor = {
        "role": document["role"],
        "round": document["round"],
        "prompt_digest": execution["prompt_digest"],
        "input_digest": execution["input_digest"],
        "specification_id": document["specification_id"],
        "specification_digest": document["specification_digest"],
    }
    if descriptor != expected_descriptor:
        raise RunConflict("Factory artifact differs from its registered role dispatch.")
    session = intent["data"]["session"]
    manifest = session["manifest"]
    receipt = result["receipt"]
    expected = {
        "adapter": session["adapter"],
        "model": session["account"]["model"],
        "agent_version": result["agent_version"],
        "session_id": result["session_id"],
        "operation_id": manifest["operation_id"],
        "execution_identity": receipt.get("execution_identity"),
        "input_source_digest": manifest["source_digest"],
        "input_digest": manifest["context_digest"],
        "started_at": receipt["started_at"],
        "ended_at": receipt["ended_at"],
    }
    if any(execution[key] != value for key, value in expected.items()):
        raise RunConflict(
            "Factory execution differs from its immutable native receipt."
        )
    if (
        receipt["run_id"] != str(run.pk)
        or manifest["run_id"] != str(run.pk)
        or receipt["operation_id"] != manifest["operation_id"]
    ):
        raise RunConflict("Factory execution belongs to another run or operation.")
    if session["agent_version"] != result["agent_version"]:
        raise RunConflict("Factory native CLI version changed during execution.")
    if document["role"] == "build":
        if (
            workspace["agent_intent_id"] != execution["intent_id"]
            or receipt["export_digest"] != workspace["export_digest"]
        ):
            raise RunConflict(
                "Factory build differs from its verified workspace export."
            )
    elif (
        receipt["source_digest"] != document["source_digest"]
        or receipt["export_digest"] != document["source_digest"]
    ):
        raise RunConflict("A read-only factory role changed its inspected candidate.")


def bind_checks(run, document):
    payload = document["payload"]
    validation = evidence(run, payload["validation_id"], "native_validation")
    intent = evidence(run, validation["intent_id"], "native_intent")
    workspace_id = (
        run.evidence.filter(kind="native_workspace").latest("pk").operation_id
    )
    expected = {key: document[key] for key in ("tree_sha", "source_digest")}
    if any(validation[key] != value for key, value in expected.items()):
        raise RunConflict("Factory checks differ from their native validation source.")
    if (
        validation["state"] != payload["state"]
        or validation["run_id"] != str(run.pk)
        or intent["kind"] != "validation"
        or intent["context_id"] != document["context_id"]
        or intent["context_digest"] != document["context_digest"]
        or intent["data"]["workspace_id"] != str(workspace_id)
    ):
        raise RunConflict("Factory checks differ from their saved outcome/context.")


def bind_references(run, document):
    identities = document["payload"]["artifact_ids"]
    if len(set(identities)) != len(identities):
        raise RunConflict("Decision artifact references must be unique.")
    entries = list(run.factory_artifacts.filter(pk__in=identities))
    if len(entries) != len(identities):
        raise RunConflict("Decision must reference saved artifacts of this run.")
    keys = (
        "context_id",
        "specification_id",
        "source_digest",
        "tree_sha",
        "base_sha",
        "head_sha",
        "configuration_digest",
        "round",
    )
    if any(
        any(entry.document[key] != document[key] for key in keys) for entry in entries
    ):
        raise RunConflict(
            "Decision references must match its current specification, candidate and round."
        )


@transaction.atomic
def save_artifact(*, runner, run_id, claim_token, operation_id, document, reason=""):
    run = locked_run(runner, run_id, claim_token)
    document = checked_document(document)
    require_safe_document(run, document)
    secrets = [str(claim_token), str(run.item.claim_token), run.item.claim_token.hex]
    if redact_payload(document, secrets) != document:
        raise ValidationError(
            {"document": "Factory artifacts cannot contain claim secrets."}
        )
    if not isinstance(reason, str) or len(reason) > 2000:
        raise ValidationError({"reason": "Record a bounded reason."})
    if (
        redact_run_payload(run, reason) != reason
        or redact_payload(reason, secrets) != reason
    ):
        raise ValidationError({"reason": "Factory reasons cannot contain secrets."})
    if document["run_id"] != str(run.pk) or document["operation_id"] != str(
        operation_id
    ):
        raise RunConflict("Factory envelope belongs to another run or operation.")
    previous = previous_operation(FactoryArtifact, run, operation_id, document, reason)
    if previous is not None:
        return previous
    if run.configuration_snapshot.get("execution_kind") != "native":
        raise RunConflict("Factory artifacts require an owned native run.")
    from .native_services import authorize, lock_scope

    run, configuration = lock_scope(run, claim_token)
    authorize(run, configuration)
    if document["configuration_digest"] != native.digest(run.configuration_snapshot):
        raise RunConflict("Factory artifact differs from the frozen run configuration.")
    context = ContextSnapshot.objects.filter(run=run, pk=document["context_id"]).first()
    if (
        context is None
        or str(context.pk) != run.context_reference
        or context.digest != document["context_digest"]
    ):
        raise RunConflict("Factory artifact requires the current owned context.")
    specification = None
    if document["specification_id"] is not None:
        specification = PlanVersion.objects.filter(
            run=run, context=context, pk=document["specification_id"]
        ).first()
        if (
            specification is None
            or specification.digest != document["specification_digest"]
            or str(specification.pk) != run.plan_reference
        ):
            raise RunConflict(
                "Factory artifact requires the current owned specification."
            )
    if run.factory_artifacts.filter(
        context=context, round=document["round"], role=document["role"]
    ).exists():
        raise RunConflict("This factory role/round already has an immutable result.")
    workspace = bind_source(run, document, context)
    if document["role"] in contract.AGENT_ROLES:
        bind_execution(run, document, context, workspace)
    elif document["role"] == "checks":
        bind_checks(run, document)
    if document["role"] == "decision":
        bind_references(run, document)
    latest = run.factory_artifacts.last()
    return FactoryArtifact.objects.create(
        run=run,
        context=context,
        specification=specification,
        sequence=latest.sequence + 1 if latest else 1,
        operation_id=operation_id,
        document=document,
        digest=native.digest(document),
        reason=reason,
        role=document["role"],
        round=document["round"],
    )
