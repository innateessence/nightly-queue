import json
from pathlib import Path

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError

from runner_control.bootstrap import bootstrap
from vibearr_runner.bootstrap_files import private_json
from work_queue.github import GitHubAPIError


class Command(BaseCommand):
    help = "Preview self-development setup; --apply selects existing credentials without enabling execution."

    def add_arguments(self, parser):
        parser.add_argument("--configuration", type=Path, required=True)
        parser.add_argument("--apply", action="store_true")
        parser.add_argument(
            "--mvp-defaults",
            action="store_true",
            help="Explicitly preview/apply30s polling,1800s agent and1200s validation budgets.",
        )

    def handle(self, *args, **options):
        try:
            path = options["configuration"]
            request = private_json(path, 65536)
            result = bootstrap(
                request, apply=options["apply"], mvp_defaults=options["mvp_defaults"]
            )
        except (OSError, ValueError, ValidationError, GitHubAPIError) as exc:
            message = (
                "; ".join(exc.messages)
                if isinstance(exc, ValidationError)
                else str(exc)
            )
            raise CommandError(message) from None
        self.stdout.write(json.dumps(result, indent=2, sort_keys=True))
        if options["apply"] and result["conflicts"]:
            raise CommandError(
                "Setup conflicts must be resolved explicitly; no changes were applied."
            )
