"""Validate native delivery receipts from the trusted supervisor."""

from django.core.exceptions import ValidationError

from .native_services import validate_delivery


def validate_success(run, result_url):
    evidence = run.evidence.filter(kind="verified_delivery").last()
    if evidence is None:
        raise ValidationError(
            {"status": "Success requires verified native delivery evidence."}
        )
    delivery = validate_delivery(run, evidence.payload)
    if result_url != delivery["delivery"]["pull_request"]["url"]:
        raise ValidationError(
            {"status": "Result URL must match the verified delivered pull request."}
        )
