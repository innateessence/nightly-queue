import argparse
import os
import signal
import time

from .config import Config
from .http import APIError
from .runner import Runner


def stop(signum, frame):
    raise KeyboardInterrupt


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run queued coding tasks with Codex.")
    options = parser.add_mutually_exclusive_group()
    options.add_argument(
        "--check",
        action="store_true",
        help="Check configuration and connectivity without claiming work.",
    )
    options.add_argument(
        "--once", action="store_true", help="Process at most one item."
    )
    options.add_argument(
        "--discard-interrupted",
        action="store_true",
        help="Archive a stopped, requeued run after checking the API.",
    )
    args = parser.parse_args(argv)
    os.umask(0o077)
    signal.signal(signal.SIGTERM, stop)
    try:
        runner = Runner(Config.from_env())
        with runner.journal.locked():
            if args.discard_interrupted:
                runner.recover(discard=True)
                return 0
            runner.preflight()
            if args.check:
                print(
                    "Runner configuration, queue authentication, GitHub authentication, Git, and Codex checked."
                )
                return 0
            runner.recover()
            while True:
                worked = runner.once()
                if args.once:
                    return 0
                if not worked:
                    time.sleep(runner.config.poll_seconds)
    except KeyboardInterrupt:
        print(
            "Runner stopped. Interrupted work is retained for reconciliation.",
            flush=True,
        )
        return 0
    except (RuntimeError, ValueError, OSError, KeyError, TypeError) as exc:
        # API and task errors deliberately omit response bodies and credentials.
        message = (
            str(exc)
            if isinstance(exc, (APIError, RuntimeError))
            else "Invalid runner configuration or state."
        )
        print(f"coding-runner: {message}", flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
