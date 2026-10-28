from django.core.management.base import BaseCommand, CommandError

from projects.models import Project
from work_queue.services import backfill_projects


class Command(BaseCommand):
    help = "Assign legacy items to an existing project before applying migration 0003."

    def add_arguments(self, parser):
        parser.add_argument("--project", type=int, required=True)
        group = parser.add_mutually_exclusive_group(required=True)
        group.add_argument("--items", type=int, nargs="+")
        group.add_argument(
            "--all", action="store_true", help="Assign all items without a project."
        )

    def handle(self, *args, **options):
        project_id = options["project"]
        item_ids = options["items"]
        try:
            count = backfill_projects(project_id=project_id, item_ids=item_ids)
        except Project.DoesNotExist as exc:
            raise CommandError("Choose an existing project ID.") from exc
        message = f"Assigned {count} item(s) to project #{project_id}."
        self.stdout.write(self.style.SUCCESS(message))
