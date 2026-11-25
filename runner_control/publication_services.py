"""Read-only provider authorization and owned native publication lookups."""

from functools import wraps

from django.views.decorators.debug import sensitive_variables

from credentials.models import Credential
from vibearr_runner.delivery import DeliveryError
from vibearr_runner.merge_github import MergeGitHub, MergeRepair, MergeWait

from .services import RunConflict


def provider_checked(operation):
    @wraps(operation)
    def call(*args, **kwargs):
        try:
            return operation(*args, **kwargs)
        except (DeliveryError, MergeRepair, MergeWait) as exc:
            raise RunConflict(str(exc)) from None

    return call


def workspace_entry(run, operation_id):
    entry = run.evidence.filter(
        operation_id=operation_id, kind="native_workspace"
    ).first()
    if entry is None:
        raise RunConflict("Revision must reference an owned inspected workspace.")
    return entry


def latest_workspace(run):
    entry = run.evidence.filter(kind="native_workspace").last()
    if entry is None:
        raise RunConflict("Inspect a native workspace before publication.")
    return entry


@sensitive_variables()
def provider(run):
    from .configuration import configuration_document

    reference = run.configuration_snapshot["effective"]["repository_credential_id"]
    document = configuration_document(run.runner)
    current = next(
        (
            entry
            for entry in document["projects"]
            if entry["project_id"] == run.item.project_id
        ),
        None,
    )
    if current is None or current["effective"]["repository_credential_id"] != reference:
        raise RunConflict(
            "Selected repository credential changed; retain factual reconciliation ownership."
        )
    selected = Credential.objects.filter(
        pk=reference, provider="github", kind="token", revoked_at__isnull=True
    ).first()
    metadata = next(
        (
            entry
            for entry in run.configuration_snapshot["credential_references"]
            if entry["id"] == reference
        ),
        None,
    )
    if (
        selected is None
        or metadata is None
        or metadata["secret_version"] != selected.secret_version
    ):
        raise RunConflict("Selected repository credential is unavailable or changed.")
    return MergeGitHub(selected.get_secret()["secret"], read_only=True)
