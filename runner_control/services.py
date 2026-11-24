from copy import deepcopy

import hashlib
import json
import re
import uuid

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.crypto import constant_time_compare
from django.views.decorators.debug import sensitive_variables

from credentials.redaction import redact_run_payload

from work_queue.models import QueueItem
from work_queue.services import claim_item, claimable_items, finish_item

from .auth import token_digest
from .configuration import locked_configuration_snapshot
from .models import Run, RunEvidence, RunnerController, RunnerIdentity

MAX_EVIDENCE_BYTES = 65536
MAX_EVIDENCE_ENTRIES = 1000


class RunConflict(Exception):
    pass


class RunNotFound(Exception):
    pass


def lock_controller(runner=None):
    controller, _ = RunnerController.objects.select_for_update().get_or_create(pk=1)
    # Also obtains a write lock on SQLite for serialized ownership transitions.
    controller.generation += 1
    controller.save(update_fields=["generation"])
    if runner is None:
        return
    current = RunnerIdentity.objects.select_for_update().get(pk=runner.pk)
    proof_changed = not constant_time_compare(
        current.controller_digest, runner.controller_digest
    )
    proven = getattr(runner, "controller_proven", False)
    if proof_changed or current.controller_digest and not proven:
        raise RunConflict(
            "Controller volume proof is required; operator reconciliation is required."
        )
    if not current.enabled or not constant_time_compare(
        current.token_digest, runner.token_digest
    ):
        raise RunConflict("Runner authentication changed; authenticate again.")


def owned_run(runner, run_id):
    try:
        return Run.objects.select_related("item__project").get(pk=run_id, runner=runner)
    except Run.DoesNotExist:
        raise RunNotFound("Run not found.") from None


def verify_active(run, claim_token, *, allow_cancellation=False):
    if run.state != Run.State.ACTIVE or run.item is None:
        raise RunConflict("Run no longer owns active work.")
    if run.cancellation_requested_at and not allow_cancellation:
        raise RunConflict("Cancellation requested; stop execution before continuing.")
    item = run.item
    if item.status != QueueItem.Status.RUNNING or item.claim_token is None:
        raise RunConflict("Run no longer owns active work.")
    if not constant_time_compare(token_digest(str(item.claim_token)), run.claim_digest):
        raise RunConflict("Run no longer owns active work.")
    if not constant_time_compare(str(item.claim_token), str(claim_token)):
        raise RunConflict("Item is not running under this claim.")


@transaction.atomic
def claim_run(*, runner, run_id, configuration_revision="", project_ids=None):
    lock_controller(runner)
    previous = Run.objects.filter(pk=run_id).first()
    if previous is not None:
        if previous.runner_id != runner.pk:
            raise RunNotFound("Run not found.")
        return owned_run(runner, run_id)
    if project_ids is not None and (
        not isinstance(project_ids, list)
        or len(project_ids) > 1000
        or any(type(value) is not int or not 0 < value < 2**63 for value in project_ids)
        or len(set(project_ids)) != len(project_ids)
    ):
        raise ValidationError(
            {"project_ids": "Use at most1000 unique positive project IDs."}
        )
    if Run.objects.filter(state=Run.State.ACTIVE).exists():
        raise RunConflict(
            "An active run already owns the controller; reconcile it first."
        )
    skipped_projects = set()
    while True:
        eligible = claimable_items()
        if project_ids is not None:
            eligible = eligible.filter(project_id__in=project_ids)
        candidate = (
            eligible.exclude(project_id__in=skipped_projects)
            .select_related("project")
            .first()
        )
        if candidate is None:
            return Run.objects.create(
                id=run_id,
                runner=runner,
                state=Run.State.EMPTY,
                finished_at=timezone.now(),
            )
        try:
            revision, snapshot = locked_configuration_snapshot(
                candidate.project, configuration_revision, runner
            )
        except ValidationError as exc:
            if "configuration" not in getattr(exc, "message_dict", {}):
                raise
            skipped_projects.add(candidate.project_id)
            continue
        defaults = snapshot.get("global", {})
        if not defaults.get("enabled", True) or defaults.get("paused", False):
            skipped_projects.add(candidate.project_id)
            continue
        item = claim_item(candidate, execution_ref=f"runner:{run_id}")
        if item is None:
            continue
        return Run.objects.create(
            id=run_id,
            runner=runner,
            item=item,
            state=Run.State.ACTIVE,
            active_slot=1,
            configuration_revision=revision,
            configuration_snapshot=snapshot,
            source_snapshot=deepcopy(item.source_snapshot),
            claim_digest=token_digest(str(item.claim_token)),
        )


@transaction.atomic
def recover_claim(*, runner, run_id):
    lock_controller(runner)
    run = owned_run(runner, run_id)
    token = run.item.claim_token if run.item else None
    verify_active(run, token, allow_cancellation=True)
    return run


def json_digest(value):
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


@sensitive_variables()
def redact_payload(value, secrets, depth=0, *, preserve_secret_versions=False):
    if depth > 20:
        raise ValidationError({"body": "Reduce JSON nesting to at most 20 levels."})
    if isinstance(value, str):
        if "\x00" in value:
            raise ValidationError(
                {"body": "JSON strings cannot contain null characters."}
            )
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            raise ValidationError({"body": "Send valid Unicode strings."}) from None
        for secret in secrets:
            if secret:
                value = re.sub(re.escape(secret), "[redacted]", value, flags=re.I)
        return value
    if isinstance(value, list):
        return [
            redact_payload(
                item,
                secrets,
                depth + 1,
                preserve_secret_versions=preserve_secret_versions,
            )
            for item in value
        ]
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            sensitive = re.search(
                r"token|secret|password|authorization|private.?key|encrypted_payload",
                key,
                re.I,
            )
            safe_key = redact_payload(
                key,
                secrets,
                depth + 1,
                preserve_secret_versions=preserve_secret_versions,
            )
            metadata = key == "secret_version" and type(item) is int
            sensitive = sensitive and not (preserve_secret_versions and metadata)
            result[safe_key] = (
                "[redacted]"
                if sensitive
                else redact_payload(
                    item,
                    secrets,
                    depth + 1,
                    preserve_secret_versions=preserve_secret_versions,
                )
            )
        return result
    return value


def append_evidence(run, operation_id, kind, payload):
    previous = RunEvidence.objects.filter(run=run, operation_id=operation_id).first()
    if previous is not None:
        if previous.kind != kind or json_digest(previous.payload) != json_digest(
            payload
        ):
            raise RunConflict(
                "This evidence operation ID already has different content."
            )
        return previous, False
    if run.evidence.count() >= MAX_EVIDENCE_ENTRIES:
        raise RunConflict("Run evidence limit reached.")
    return (
        RunEvidence.objects.create(
            run=run, operation_id=operation_id, kind=kind, payload=payload
        ),
        True,
    )


@transaction.atomic
@sensitive_variables()
def record_evidence(
    *, runner, run_id, claim_token, operation_id, kind, payload, progress=None
):
    if kind == "publication_observation":
        raise RunConflict(
            "Publication observations are written only by trusted provider reconciliation."
        )
    lock_controller(runner)
    run = owned_run(runner, run_id)
    from vibearr_runner.native_contract import ARTIFACT_KINDS, FACT_KINDS

    intent_data = payload.get("data") if isinstance(payload, dict) else None
    completion = (
        kind == "native_intent"
        and isinstance(payload, dict)
        and payload.get("kind") == "merge"
        and isinstance(intent_data, dict)
        and intent_data.get("stage") in {"comment", "close", "delete"}
    )
    reporting = (
        completion
        or run.configuration_snapshot.get("execution_kind") == "native"
        and kind in FACT_KINDS | {"verified_delivery"}
    )
    verify_active(run, claim_token, allow_cancellation=reporting)
    progress_fields = [field for field in (progress or {}) if field != "stage"]
    values = [progress[field] for field in progress_fields]
    kind, payload, values = redact_run_payload(run, [kind, payload, values])
    if progress is not None:
        progress.update(zip(progress_fields, values))
    previous = run.evidence.filter(operation_id=operation_id).first()
    if kind in ARTIFACT_KINDS and previous is None:
        from .native_services import bind_artifact

        run = bind_artifact(run, claim_token, kind, payload, operation_id)
    if kind == "verified_delivery" and previous is None:
        from .delivery import validate_delivery

        validate_delivery(run, payload)
        if run.configuration_snapshot.get("execution_kind") == "native" and payload.get(
            "operation_id"
        ) != str(operation_id):
            raise RunConflict(
                "Native delivery evidence must use its saved metadata operation identity."
            )
    evidence, created = append_evidence(run, operation_id, kind, payload)
    if created and progress is not None:
        for field, value in progress.items():
            setattr(run, field, value)
        if run.configuration_snapshot.get(
            "execution_kind"
        ) != "native" and progress.get("stage") in {
            Run.Stage.IMPLEMENTATION,
            Run.Stage.VALIDATION,
            Run.Stage.DELIVERY,
        }:
            from .plan_services import require_saved_plan

            require_saved_plan(run)
        run.save(update_fields=[*progress, "updated_at"])
    return evidence


def validate_success(run, result_url):
    from .delivery import validate_success as validate_delivery_success

    validate_delivery_success(run, result_url)


@transaction.atomic
@sensitive_variables()
def finish_run(
    *, runner, run_id, claim_token, status, result_url="", error_summary="", outcome=""
):
    lock_controller(runner)
    run = owned_run(runner, run_id)
    from .native_services import registered, known_delivery_url

    native = registered(run)
    if native and status == QueueItem.Status.FAILED:
        result_url = result_url or known_delivery_url(run)
    if native and not outcome:
        outcome = (
            "delivered"
            if status == QueueItem.Status.SUCCEEDED
            else "canceled" if run.cancellation_requested_at else "failed"
        )
    result = {
        "status": status,
        "result_url": result_url,
        "error_summary": error_summary,
    }
    if outcome:
        if outcome not in {
            "delivered",
            "merged",
            "failed",
            "canceled",
            "no_change",
            "needs_info",
            "rejected",
        }:
            raise ValidationError({"outcome": "Choose a native terminal outcome."})
        result["outcome"] = outcome
        if outcome == "no_change" and not error_summary.strip():
            raise ValidationError(
                {"error_summary": "Explain why native work made no changes."}
            )
    digest = json_digest(result)
    if run.state == Run.State.COMPLETED:
        if (
            token_digest(str(claim_token)) != run.claim_digest
            or digest != run.finish_digest
        ):
            raise RunConflict(
                "This run already completed with a different claim or result."
            )
        return run
    verify_active(run, claim_token, allow_cancellation=True)
    if outcome in {"needs_info", "rejected"}:
        from .factory_native import terminal_outcome

        terminal_outcome(run, outcome)
    merge_race = (
        native
        and outcome == "merged"
        and status == QueueItem.Status.SUCCEEDED
        and run.evidence.filter(kind="native_merge").exists()
    )
    if (
        run.cancellation_requested_at
        and not merge_race
        and (status != QueueItem.Status.FAILED or not run.cancellation_confirmed_at)
    ):
        raise RunConflict(
            "Cancellation requires confirmed termination and a failed result."
        )
    result_url, error_summary = redact_run_payload(run, [result_url, error_summary])
    result.update(result_url=result_url, error_summary=error_summary)
    if native:
        from .native_services import lock_scope, require_termination

        run, _ = lock_scope(run, claim_token, reporting=True)
        require_termination(run)
        if run.evidence.filter(kind="native_merge").exists() and (
            status != QueueItem.Status.SUCCEEDED or outcome != "merged"
        ):
            raise RunConflict(
                "Actual merged work must retain its merged successful outcome."
            )
        if (
            run.cancellation_requested_at
            and not merge_race
            and (status != QueueItem.Status.FAILED or not run.cancellation_confirmed_at)
        ):
            raise RunConflict(
                "Cancellation requires confirmed termination and a failed result."
            )
        if (
            outcome in {"delivered", "merged"}
            and status != QueueItem.Status.SUCCEEDED
            or outcome not in {"delivered", "merged"}
            and status != QueueItem.Status.FAILED
        ):
            raise RunConflict("Native outcome differs from its queue result.")
        if run.cancellation_requested_at and outcome != "canceled" and not merge_race:
            raise RunConflict(
                "Canceled native work must retain its cancellation outcome."
            )
        if (
            outcome == "no_change"
            and run.evidence.filter(kind="native_delivery").exists()
        ):
            raise RunConflict("Published native work cannot be reported as no change.")
        mode = run.configuration_snapshot["execution_policy"]["delivery_mode"]
        if status == QueueItem.Status.SUCCEEDED and outcome != (
            "merged" if mode == "merge" else "delivered"
        ):
            raise RunConflict(
                "Successful native outcome must match its configured delivery mode."
            )
    if status == QueueItem.Status.SUCCEEDED:
        validate_success(run, result_url)
    updated = finish_item(
        item_id=run.item_id,
        claim_token=claim_token,
        runner_run=run,
        status=status,
        result_url=result_url,
        error_summary=error_summary,
    )
    if not updated:
        raise RunConflict("Item is not running under this claim.")
    run.state = Run.State.COMPLETED
    run.active_slot = None
    run.stage = Run.Stage.FINISHED
    run.wait_reason = ""
    run.finished_at = timezone.now()
    run.result_status = status
    run.result_url = result_url
    run.error_summary = error_summary
    run.finish_digest = digest
    run.native_outcome = outcome
    run.save()
    if native and run.retry_requested_at:
        from .native_services import complete_retry

        complete_retry(run)
    return owned_run(runner, run_id)


def uuid_field(data, field):
    try:
        value = data[field]
        if not isinstance(value, str):
            raise ValueError
        return uuid.UUID(value)
    except (KeyError, ValueError):
        raise ValidationError({field: "Send a UUID string."}) from None
