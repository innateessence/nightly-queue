import math
from datetime import timedelta
from uuid import UUID

from django.core.exceptions import ValidationError
from django.db import transaction
from django.http import JsonResponse
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from nightly_queue.api import read_body

from .auth import runner_api_view
from .gate_models import RunnerGateEvidence
from .models import Run, RunnerIdentity


REPORT_FIELDS = {
    "schema_version",
    "allowed",
    "reasons",
    "checked_at",
    "checkpoint",
    "run_id",
    "configuration_revision",
    "interval",
    "next_boundary",
    "next_check_at",
    "reserve_enabled",
    "minimum_remaining_usage_percent",
    "readings",
    "tasks_started",
}
REASONS = {
    "release_disabled",
    "disabled",
    "paused",
    "outside_window",
    "resume_owned_run_first",
    "ownership_unknown",
    "claim_accounting_unknown",
    "reconciliation_required",
    "capacity",
    "window_limit",
    "usage_missing_accounts",
    "usage_missing",
    "usage_unsupported",
    "usage_unavailable",
    "usage_invalid",
    "usage_stale",
    "usage_reserve",
}
STATUSES = {
    "ready",
    "missing",
    "unsupported",
    "unavailable",
    "invalid",
    "stale",
    "reserve",
}


def invalid():
    raise ValidationError(
        {"report": "Send a bounded gate report using schema version 1."}
    )


def keys(value, fields):
    if not isinstance(value, dict) or set(value) != fields:
        invalid()


def text(value, limit=120):
    if not isinstance(value, str) or not value or len(value) > limit:
        invalid()


def timestamp(value, nullable=False):
    if nullable and value is None:
        return None
    if not isinstance(value, str) or len(value) > 40:
        invalid()
    try:
        parsed = parse_datetime(value)
    except ValueError:
        invalid()
    if parsed is None or timezone.is_naive(parsed):
        invalid()
    return parsed


def percent(value, nullable=False):
    if nullable and value is None:
        return
    if (
        type(value) not in (int, float)
        or not math.isfinite(value)
        or not 0 <= value <= 100
    ):
        invalid()


def validate_report(report):
    keys(report, REPORT_FIELDS)
    if report["schema_version"] != 1 or type(report["allowed"]) is not bool:
        invalid()
    if type(report["reserve_enabled"]) is not bool:
        invalid()
    if not isinstance(report["reasons"], list) or len(report["reasons"]) > len(REASONS):
        invalid()
    if any(
        not isinstance(reason, str) or reason not in REASONS
        for reason in report["reasons"]
    ):
        invalid()
    if report["allowed"] != (not report["reasons"]):
        invalid()
    if report["checkpoint"] not in {"claim", "stage", "model", "periodic"}:
        invalid()
    text(report["configuration_revision"], 64)
    if report["run_id"] is not None:
        try:
            UUID(report["run_id"])
        except (ValueError, TypeError, AttributeError):
            invalid()
    if report["checkpoint"] != "claim" and report["run_id"] is None:
        invalid()
    checked = timestamp(report["checked_at"])
    if checked > timezone.now() + timedelta(seconds=5):
        invalid()
    timestamp(report["next_boundary"], nullable=True)
    if timestamp(report["next_check_at"]) < checked:
        invalid()
    percent(report["minimum_remaining_usage_percent"])
    if (
        type(report["tasks_started"]) is not int
        or not 0 <= report["tasks_started"] <= 2**63 - 1
    ):
        invalid()
    interval = report["interval"]
    if interval is not None:
        keys(interval, {"id", "start", "end"})
        text(interval["id"])
        if timestamp(interval["start"]) >= timestamp(interval["end"]):
            invalid()
    readings = report["readings"]
    if not isinstance(readings, list) or len(readings) > 128:
        invalid()
    for reading in readings:
        validate_reading(reading)
    return checked


def validate_reading(reading):
    keys(reading, {"backend", "credential_id", "status", "windows"})
    text(reading["backend"], 30)
    reference = reading["credential_id"]
    if reference is not None and (type(reference) is not int or reference < 1):
        invalid()
    if reading["status"] not in STATUSES:
        invalid()
    windows = reading["windows"]
    if not isinstance(windows, list) or len(windows) > 32:
        invalid()
    for window in windows:
        keys(
            window,
            {
                "window_id",
                "remaining_percent",
                "observed_at",
                "reset_at",
                "unit",
                "status",
            },
        )
        text(window["window_id"], 100)
        percent(window["remaining_percent"], nullable=True)
        timestamp(window["observed_at"])
        timestamp(window["reset_at"], nullable=True)
        text(window["unit"], 30)
        if window["status"] not in STATUSES:
            invalid()


def evidence_data(evidence):
    if evidence is None:
        return None
    return {"report": evidence.report, "received_at": evidence.received_at.isoformat()}


@runner_api_view("GET")
def gates(request):
    evidence = RunnerGateEvidence.objects.filter(runner=request.runner_identity).first()
    return JsonResponse({"schema_version": 1, "evidence": evidence_data(evidence)})


@runner_api_view("POST")
def report(request):
    if len(request.body) > 65536:
        invalid()
    try:
        data = read_body(request, ["report"])
        checked = validate_report(data.get("report"))
        run_id = data["report"]["run_id"]
        if (
            run_id
            and not Run.objects.filter(
                pk=run_id, runner=request.runner_identity, state=Run.State.ACTIVE
            ).exists()
        ):
            raise ValidationError(
                {"report": "Report checkpoints only for an owned active run."}
            )
    except (TypeError, RecursionError):
        invalid()
    with transaction.atomic():
        RunnerIdentity.objects.select_for_update().get(pk=request.runner_identity.pk)
        evidence = RunnerGateEvidence.objects.filter(
            runner=request.runner_identity
        ).first()
        if (
            evidence is not None
            and checked == evidence.checked_at
            and evidence.report != data["report"]
        ):
            return JsonResponse(
                {"error": "Gate timestamp already has different evidence."}, status=409
            )
        if evidence is None or checked > evidence.checked_at:
            evidence, _ = RunnerGateEvidence.objects.update_or_create(
                runner=request.runner_identity,
                defaults={"report": data["report"], "checked_at": checked},
            )
    return JsonResponse({"schema_version": 1, "evidence": evidence_data(evidence)})
