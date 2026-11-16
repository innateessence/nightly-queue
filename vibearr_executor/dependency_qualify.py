"""A restart-independent, bounded qualification process for one derived image."""

import argparse
import json
from pathlib import Path
import signal
import time

from .contracts import canonical
from .qualify import qualify, smoke_profiles
from .runtime import durable


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--deadline", type=float, required=True)
    parser.add_argument("--deny-ip", action="append", default=[])
    args = parser.parse_args()
    profile = json.loads(args.profile.read_bytes())
    from .validation_profile import ValidationProfile

    selected = ValidationProfile.parse(profile)
    profiles = smoke_profiles(selected.image, selected.database_image)
    profiles.extend(selected.executor_profiles())

    def stop(_signum, _frame):
        raise InterruptedError("Derived-image qualification stopped.")

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGALRM, stop)
    signal.setitimer(signal.ITIMER_REAL, max(0.1, args.deadline - time.time()))
    try:
        qualify(args.state, profiles, denied_ips=tuple(args.deny_ip))
    except Exception as exc:
        durable(
            args.state.parent / "qualification-exit.json",
            canonical({"success": False, "error": type(exc).__name__}),
        )
        raise
    else:
        durable(
            args.state.parent / "qualification-exit.json", canonical({"success": True})
        )


if __name__ == "__main__":
    main()
