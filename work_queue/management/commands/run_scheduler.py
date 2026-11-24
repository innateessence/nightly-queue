import logging
import signal
import threading
import time
from pathlib import Path

from django.core.management.base import BaseCommand
from django.db import DatabaseError, close_old_connections

from work_queue.import_jobs import tick


class Command(BaseCommand):
    help = "Run durable scheduled and manual issue imports."

    def handle(self, *args, **options):
        stop = threading.Event()
        for signum in (signal.SIGTERM, signal.SIGINT):
            signal.signal(signum, lambda *_: stop.set())
        while not stop.is_set():
            try:
                close_old_connections()
                Path("/tmp/vibearr-scheduler-heartbeat").write_text(str(time.time()))
                tick()
                from runner_control.publication_observations import (
                    observe_delivered_publications,
                )

                observe_delivered_publications(limit=1)
                Path("/tmp/vibearr-scheduler-heartbeat").write_text(str(time.time()))
            except DatabaseError:
                logging.getLogger(__name__).warning(
                    "Scheduler database unavailable; retrying."
                )
            stop.wait(5)
