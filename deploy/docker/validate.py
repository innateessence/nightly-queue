"""Validate an exact disposable Git snapshot through the worker APIs."""

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time
from uuid import uuid4

from vibearr_executor.client import Client
from vibearr_executor.validation_profile import ValidationProfile
from vibearr_runner.native_validation import (
    CandidateSource,
    DependencyBuilder,
    ValidationRunner,
)
from vibearr_runner.policy import ExecutionPolicy, default_execution_policy


ROOT = Path("/run/vibearr/runner")


def main():
    client = Client(
        "http://worker-gateway:8791",
        (ROOT / "control-token").read_text().strip(),
        timeout=180,
    )
    profile = ValidationProfile.parse(
        json.loads((ROOT / "validation-profile.json").read_bytes())
    )
    checkout = Path("/tmp/candidate")
    shutil.copytree("/app", checkout)
    if "--dependency" in sys.argv:
        with (checkout / "requirements.txt").open("a") as stream:
            stream.write("\nsix==1.17.0\n")

    def git(*arguments):
        return (
            subprocess.check_output(
                [
                    "git",
                    "-c",
                    "core.hooksPath=/dev/null",
                    "-c",
                    "user.name=Container validation",
                    "-c",
                    "user.email=validation@invalid",
                    "-C",
                    str(checkout),
                    *arguments,
                ],
                stderr=subprocess.DEVNULL,
            )
            .decode()
            .strip()
        )

    git("init", "-q")
    git("add", ".")
    git("commit", "-qm", "Freeze disposable validation candidate")
    source = CandidateSource.from_git(checkout, git("rev-parse", "HEAD"))
    policy = default_execution_policy()
    policy["validation_profile"] = profile.profile_id
    policy = ExecutionPolicy.from_data(policy)
    run_id = str(uuid4())
    deadline = (datetime.now(timezone.utc) + timedelta(minutes=55)).isoformat()
    provenance = None
    if "--dependency" in sys.argv:
        builder = DependencyBuilder(client)
        request = builder.prepare(
            run_id=run_id,
            build_id=str(uuid4()),
            source=source,
            profile=profile,
            policy=policy,
            deadline=deadline,
        )
        builder.start(request)
        expires = time.monotonic() + 1200
        while True:
            observed = builder.status(request)
            if observed["state"] == "ready":
                profile = ValidationProfile.parse(observed["profile"])
                provenance = observed["provenance"]
                print(
                    json.dumps(
                        {
                            "dependencies": "ready",
                            "artifact": provenance["dependency_digest"],
                        }
                    ),
                    flush=True,
                )
                break
            if (
                observed["state"] in {"failed", "unknown", "canceled"}
                or time.monotonic() >= expires
            ):
                print(
                    client.logs(request["build_id"]).decode(errors="replace"),
                    file=sys.stderr,
                )
                raise RuntimeError(
                    "Dependency artifact construction failed: " + json.dumps(observed)
                )
            time.sleep(0.5)
    runner = ValidationRunner(client)
    session = runner.prepare(
        run_id=run_id,
        attempt_id=str(uuid4()),
        source=source,
        profile=profile,
        policy=policy,
        deadline=deadline,
    )
    last = ""
    while True:
        observed = runner.start(session)
        if observed["state"] == "passed":
            print(
                json.dumps(
                    {
                        "kind": "disposable-validation-snapshot",
                        "source_digest": source.digest,
                        "tree_sha": source.tree_sha,
                        "validation": observed["validation"],
                        "dependency_provenance": provenance,
                    },
                    indent=2,
                ),
                flush=True,
            )
            return
        if observed["state"] in {"failed", "unknown"}:
            operation = observed["operation"]["operation_id"]
            print(client.logs(operation).decode(errors="replace"), file=sys.stderr)
            result = observed["operation"].get("result", {})
            detail = {key: value for key, value in result.items() if key != "artifact"}
            raise RuntimeError(
                f"Validation {observed['check_id']} failed: " + json.dumps(detail)
            )
        if last != observed.get("check_id"):
            last = observed.get("check_id")
            print(json.dumps({"check": last, "state": observed["state"]}), flush=True)
        time.sleep(0.2)


if __name__ == "__main__":
    main()
