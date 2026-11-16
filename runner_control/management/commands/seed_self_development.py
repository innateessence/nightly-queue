import json
from pathlib import Path
import re

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils.dateparse import parse_datetime

from credentials.models import Credential
from vibearr_runner.bootstrap_backlog import (
    REPOSITORY,
    REPOSITORY_ID,
    OWNER_ID,
    export_plan,
    marker,
    publish,
)
from vibearr_runner.delivery import DeliveryError
from vibearr_runner.native_github import NativeGitHub
from work_queue.models import MergeEvidence
from work_queue.services import record_verified_merge


def verified_prerequisites(github, mappings):
    if not isinstance(mappings, list) or len(mappings) > 16:
        raise ValidationError(
            "Supply at most16 explicit trusted feature/issue/PR/merge mappings."
        )
    verified = []
    features = set()
    issues = set()
    for value in mappings:
        if not isinstance(value, dict) or set(value) != {
            "feature",
            "issue",
            "pull",
            "merge_sha",
        }:
            raise ValidationError(
                "Each trusted mapping needs feature, issue, pull and merge_sha."
            )
        if any(
            type(value[field]) is not int or not 0 < value[field] < 2**63
            for field in ("issue", "pull")
        ):
            raise ValidationError("Prerequisites require positive issue and pull IDs.")
        if not isinstance(value["merge_sha"], str) or not re.fullmatch(
            r"[a-f0-9]{40}", value["merge_sha"]
        ):
            raise ValidationError(
                "Prerequisites require the expected provider merge SHA."
            )
        feature_marker = marker(value["feature"])
        if value["feature"] in features or value["issue"] in issues:
            raise ValidationError(
                "Trusted prerequisite mappings must have unique feature and issue identities."
            )
        features.add(value["feature"])
        issues.add(value["issue"])
        issue = github.request("GET", f'/repos/{REPOSITORY}/issues/{value["issue"]}')
        pull = github.pull(REPOSITORY, value["pull"])
        if (
            issue.get("number") != value["issue"]
            or issue.get("user", {}).get("id") != OWNER_ID
            or feature_marker not in (issue.get("body") or "")
            or "pull_request" in issue
        ):
            raise ValidationError(
                "The trusted mapping does not identify the owner-authored feature issue."
            )
        valid = (
            pull.get("merged") is True
            and pull.get("merge_commit_sha") == value["merge_sha"]
        )
        valid = valid and pull.get("base", {}).get("ref") == "main"
        valid = valid and all(
            pull.get(side, {}).get("repo", {}).get("id") == REPOSITORY_ID
            for side in ("base", "head")
        )
        merged_at = parse_datetime(pull.get("merged_at") or "")
        if not valid or merged_at is None or merged_at.utcoffset() is None:
            raise ValidationError(
                "Prerequisite is not a provider-verified merge into this repository's main branch."
            )
        verified.append({**value, "merged_at": merged_at})
    return verified


@transaction.atomic
def record_prerequisites(verified):
    result = []
    for value in verified:
        previous = MergeEvidence.objects.filter(
            repository_id=REPOSITORY_ID, issue_number=value["issue"]
        ).first()
        if previous:
            if (
                previous.pull_number != value["pull"]
                or previous.merge_sha != value["merge_sha"]
                or previous.merged_at != value["merged_at"]
            ):
                raise ValidationError(
                    "Provider evidence conflicts with the existing immutable prerequisite record."
                )
            result.append(previous.pk)
            continue
        evidence = record_verified_merge(
            repository_id=REPOSITORY_ID,
            issue_number=value["issue"],
            pull_number=value["pull"],
            merge_sha=value["merge_sha"],
            merged_at=value["merged_at"],
            provenance="github-api-bootstrap-v1",
            operation_id=f'vibearr-bootstrap-{value["feature"]}-{value["pull"]}-{value["merge_sha"]}',
        )
        result.append(evidence.pk)
    return result


class Command(BaseCommand):
    help = "Export plan issues, explicitly publish missing owner markers, or verify trusted merged prerequisites."

    def add_arguments(self, parser):
        parser.add_argument(
            "--plan", type=Path, default=Path("docs/autonomous-ai-queue-plan.md")
        )
        parser.add_argument("--publish", action="store_true")
        parser.add_argument("--state", type=Path)
        parser.add_argument("--credential-id", type=int)
        parser.add_argument("--record-merges", type=Path)
        parser.add_argument("--apply", action="store_true")

    def handle(self, *args, **options):
        try:
            if options["publish"] and options["record_merges"]:
                raise CommandError(
                    "Publish issues and seed completed prerequisites as separate explicit operations."
                )
            if not options["publish"] and not options["record_merges"]:
                self.stdout.write(
                    json.dumps(
                        export_plan(options["plan"].read_text()),
                        indent=2,
                        sort_keys=True,
                    )
                )
                return
            credential = Credential.objects.filter(
                pk=options["credential_id"],
                provider="github",
                kind="token",
                revoked_at__isnull=True,
            ).first()
            if credential is None:
                raise CommandError(
                    "Explicitly select an unrevoked GitHub token credential ID."
                )
            github = NativeGitHub(
                credential.get_secret()["secret"], read_only=not options["publish"]
            )
            if options["publish"]:
                if not options["apply"] or options["state"] is None:
                    raise CommandError(
                        "Publication requires --apply and a dedicated private --state journal."
                    )
                result = publish(
                    export_plan(options["plan"].read_text()), github, options["state"]
                )
            else:
                if options["record_merges"].stat().st_size > 65536:
                    raise CommandError("Trusted prerequisite manifest exceeds64KiB.")
                values = json.loads(options["record_merges"].read_text())
                verified = verified_prerequisites(github, values)
                ids = record_prerequisites(verified) if options["apply"] else []
                result = {
                    "schema_version": 1,
                    "verified_issues": [value["issue"] for value in verified],
                    "applied": options["apply"],
                    "evidence_ids": ids,
                }
            self.stdout.write(json.dumps(result, indent=2, sort_keys=True))
        except (OSError, ValueError, DeliveryError, ValidationError) as exc:
            message = (
                "; ".join(exc.messages)
                if isinstance(exc, ValidationError)
                else str(exc)
            )
            raise CommandError(message) from None
