import json
from pathlib import Path
import os

from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.core.management.base import BaseCommand, CommandError

from runner_control.bootstrap import (
    REPOSITORY_ID,
    REPOSITORY_URL,
    selected_credentials,
    validate_request,
)
from runner_control.bootstrap_diagnostics import diagnose
from vibearr_runner.bootstrap_files import private_json
from vibearr_runner.client import Client
from vibearr_runner.delivery import DeliveryError
from vibearr_runner.environment import getenv
from vibearr_runner.native_github import NativeGitHub
from work_queue.github import GitHubAPIError, repository_metadata


class Command(BaseCommand):
    help = "Consolidated read-only setup diagnostics; --online observes APIs without claims or paid auth probes."

    def add_arguments(self, parser):
        parser.add_argument("--configuration", type=Path, required=True)
        parser.add_argument("--online", action="store_true")
        parser.add_argument("--state", type=Path)
        parser.add_argument("--profile", type=Path)

    def handle(self, *args, **options):
        try:
            request = validate_request(private_json(options["configuration"], 65536))
            try:
                credentials = selected_credentials(request)
            except ValidationError:
                credentials = {}
            repository = {
                "repository_id": REPOSITORY_ID,
                "owner_id": 17239750,
                "repository_url": REPOSITORY_URL,
                "default_branch": "main",
            }
            queue = executor = github = None
            failures = {}
            state = options["state"]
            journal = (
                private_json(state / "journal.json")
                if state and (state / "journal.json").exists()
                else {}
            )
            if options["online"] and credentials:
                try:
                    repository = repository_metadata(
                        REPOSITORY_URL,
                        credentials["import"].get_secret()["secret"],
                        REPOSITORY_ID,
                    )
                except (
                    OSError,
                    ValueError,
                    ImproperlyConfigured,
                    ValidationError,
                    GitHubAPIError,
                ) as exc:
                    failures["import_provider"] = str(exc)
                try:
                    github = NativeGitHub(
                        credentials["repository"].get_secret()["secret"], read_only=True
                    )
                except (
                    ValueError,
                    ImproperlyConfigured,
                    ValidationError,
                    DeliveryError,
                ) as exc:
                    failures["provider_permissions"] = str(exc)
                try:
                    queue = Client(
                        getenv("NQ_URL", "http://vibearr:8000"),
                        os.environ.get(request["runner_token_env"], ""),
                    )
                    queue.controller_secret = journal.get("controller_secret")
                except ValueError as exc:
                    failures["api_identity"] = str(exc)
                try:
                    from vibearr_executor.client import Client as ExecutorClient

                    executor = ExecutorClient.from_environment()
                except (ImportError, OSError, ValueError) as exc:
                    failures["runtime"] = str(exc)
            result = diagnose(
                request,
                repository,
                queue=queue,
                executor=executor,
                github=github,
                journal=journal,
                state_directory=state,
                profile_path=options["profile"],
                failures=failures,
            )
        except (
            OSError,
            ValueError,
            ImproperlyConfigured,
            ValidationError,
            GitHubAPIError,
            DeliveryError,
        ) as exc:
            message = (
                "; ".join(exc.messages)
                if isinstance(exc, ValidationError)
                else str(exc)
            )
            raise CommandError(message) from None
        self.stdout.write(json.dumps(result, indent=2, sort_keys=True))
