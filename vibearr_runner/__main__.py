import argparse
import logging
from .environment import getenv
from pathlib import Path
import signal
import threading

from .client import APIError, Client
from .controller import Controller
from .journal import Journal, JournalError
from .maintenance import Maintenance


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Recoverable runner supervisor; execution requires controlled release readiness."
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
            getenv("NQ_URL", "http://vibearr:8000"),
            getenv("NQ_RUNNER_API_TOKEN", ""),
            max_bytes=8388608,
        )
        directory = Path(getenv("NQ_RUNNER_STATE_DIR", "/var/lib/coding-runner"))
        with Journal(directory) as journal:
            maintenance = Maintenance(client, journal, directory / "workspaces")
            Controller(
                client,
                journal,
                stop,
                maintenance=maintenance,
                completed=maintenance.acknowledge,
            ).run(check=arguments.check)
    except (APIError, JournalError, ValueError, OSError) as exc:
        logging.error(
            "Runner stopped (%s). Check bootstrap configuration and private journal.",
            type(exc).__name__,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
