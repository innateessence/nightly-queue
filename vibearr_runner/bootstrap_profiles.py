"""Preview or additively save reviewed operator profiles; never launch tasks."""

import argparse
import json
import os
from pathlib import Path

from .bootstrap_files import private_json
from .native_codex import codex_profile
from .native_validation import load_validation_profile


def profiles(agent_image, validation, kind, existing=()):
    from vibearr_executor.contracts import Profile

    wanted = [
        codex_profile(agent_image, kind, database_image=validation.database_image),
        *validation.executor_profiles(),
        validation.setup_profile(),
    ]
    combined = {}
    for raw in existing:
        value = Profile.parse(raw)
        if value.profile_id.startswith("smoke-"):
            raise ValueError(
                "Select the operator additions file, not the generated smoke registry; qualification owns smoke profiles."
            )
        if value.profile_id in combined:
            raise ValueError("Existing operator registry has duplicate profile IDs.")
        combined[value.profile_id] = value
    for value in wanted:
        value = Profile.parse(value.data())
        previous = combined.get(value.profile_id)
        if previous and previous.digest != value.digest:
            raise ValueError(
                f"Registered profile {value.profile_id} differs; preserve it and select a new reviewed profile explicitly."
            )
        combined[value.profile_id] = value
    return [combined[key].data() for key in sorted(combined)]


def save_new(path, values):
    content = json.dumps(values, indent=2, sort_keys=True).encode() + b"\n"
    if path.exists():
        if private_json(path) != values:
            raise ValueError(
                "Existing profile file differs; bootstrap never overwrites an operator choice."
            )
        return False
    descriptor = os.open(
        path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
    )
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    descriptor = os.open(path.parent, os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent-image", required=True)
    parser.add_argument("--validation-profile", type=Path, required=True)
    parser.add_argument("--kind", choices=("token", "session"), default="token")
    parser.add_argument("--registry", type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        validation = load_validation_profile(args.validation_profile)
        existing = private_json(args.registry) if args.registry else []
        values = profiles(args.agent_image, validation, args.kind, existing)
        if not args.apply:
            print(
                json.dumps(
                    {
                        "schema_version": 1,
                        "applied": False,
                        "profile_ids": [value["profile_id"] for value in values],
                        "agent_image": args.agent_image,
                        "validation_image": validation.image,
                        "database_image": validation.database_image,
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0
        if args.output is None:
            parser.error(
                "Explicit --apply requires an output path; existing different files are preserved."
            )
        changed = save_new(args.output, values)
        print(
            json.dumps(
                {
                    "applied": True,
                    "changed": changed,
                    "profile_count": len(values),
                    "output": str(args.output),
                }
            )
        )
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
