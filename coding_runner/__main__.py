import argparse
import logging
import os
from pathlib import Path
import signal
import threading

from .client import APIError, Client
from .controller import Controller
from .journal import Journal, JournalError


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Read-only runner supervisor; model execution is disabled."
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Authenticate and read configuration once, without claims.",
    )
    arguments = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    stop = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stop.set())
    try:
        client = Client(
            os.environ.get("NQ_URL", "http://nightly-queue:8000"),
            os.environ.get("NQ_RUNNER_API_TOKEN", ""),
        )
        directory = Path(
            os.environ.get("NQ_RUNNER_STATE_DIR", "/var/lib/coding-runner")
        )
        with Journal(directory) as journal:
            Controller(client, journal, stop).run(check=arguments.check)
    except (APIError, JournalError, ValueError, OSError) as exc:
        logging.error(
            "Runner stopped (%s). Check bootstrap configuration and private journal.",
            type(exc).__name__,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
