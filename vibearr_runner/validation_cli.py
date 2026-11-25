"""Run the trusted validation profile against an exact candidate commit."""

import argparse
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import time
from uuid import uuid4

from .native_validation import CandidateSource, ValidationRunner
from .policy import ExecutionPolicy


def run_candidate(client, source, profile, *, run_id=None, state=None, deadline=None):
    from vibearr_executor.contracts import canonical
    from vibearr_executor.validation_profile import PACKAGE_HOSTS, ValidationProfile

    run_id = run_id or str(uuid4())
    deadline = (
        deadline or (datetime.now(timezone.utc) + timedelta(minutes=55)).isoformat()
    )
    policy = replace(ExecutionPolicy(), validation_profile=profile.profile_id)
    provenance = None
    from vibearr_executor.artifacts import unpack
    from vibearr_executor.contracts import digest

    requirements = unpack(source.archive).get("requirements.txt")
    if requirements is None:
        raise RuntimeError("Candidate requirements are missing.")
    if digest(requirements[1]) != profile.dependency_digest:
        build_id = str(uuid4())
        request = {
            "schema_version": 1,
            "run_id": run_id,
            "build_id": build_id,
            "source_digest": source.digest,
            "setup_profile_id": profile.setup_profile().profile_id,
            "validation_profile": profile.data(),
            "limits": asdict(profile.setup_profile().limits),
            "deadline": deadline,
            "network_hosts": list(PACKAGE_HOSTS),
        }
        client.upload("source", source.archive)
        client.prepare_dependencies(request)
        client.dependencies(build_id, "start")
        while True:
            observed = client.dependencies(build_id, "status")
            if state:
                (state / "build-status.json").write_bytes(canonical(observed))
            if observed["state"] == "ready":
                profile = ValidationProfile.parse(observed["profile"])
                provenance = observed["provenance"]
                break
            if observed["state"] in {"failed", "unknown", "canceled"}:
                raise RuntimeError(
                    "Dependency build did not complete: "
                    + observed.get("wait_reason", observed["state"])
                )
            if datetime.now(timezone.utc) >= datetime.fromisoformat(deadline):
                client.dependencies(build_id, "stop")
                raise TimeoutError("Dependency-build deadline expired.")
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
    if state:
        (state / "session.json").write_bytes(canonical(session))
    try:
        while True:
            observed = runner.start(session)
            if observed["state"] in {"passed", "failed", "unknown"}:
                break
            if datetime.now(timezone.utc) >= datetime.fromisoformat(deadline):
                raise TimeoutError("Candidate validation deadline expired.")
            time.sleep(0.25)
        return {
            "schema_version": 1,
            "run_id": run_id,
            "commit_sha": source.commit_sha,
            "tree_sha": source.tree_sha,
            "source_digest": source.digest,
            "profile": profile.data(),
            "dependency_provenance": provenance,
            "result": observed,
        }
    finally:
        for entry in session["operations"]:
            operation = entry["manifest"]["operation_id"]
            client.stop(operation)
            if state:
                content = bytearray()
                while len(content) < 1024 * 1024:
                    chunk = client.logs(operation, len(content))
                    if not chunk:
                        break
                    content.extend(chunk)
                (state / (entry["id"] + ".log")).write_bytes(content)


def main():
    from vibearr_executor.contracts import canonical
    from vibearr_executor.client import Client
    from vibearr_executor.validation_profile import ValidationProfile

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkout", type=Path, required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    args = parser.parse_args()
    profile = ValidationProfile.parse(json.loads(args.profile.read_bytes()))
    args.state.mkdir(mode=0o700, parents=True, exist_ok=True)
    output = args.state / "candidate"
    output.mkdir(mode=0o700)
    source = CandidateSource.from_git(args.checkout, args.commit)
    report = run_candidate(Client.from_environment(), source, profile, state=output)
    args.evidence.write_bytes(canonical(report) + b"\n")
    print(
        json.dumps(
            {
                "state": report["result"]["state"],
                "commit_sha": source.commit_sha,
                "tree_sha": source.tree_sha,
                "evidence": str(args.evidence),
            }
        )
    )
    return 0 if report["result"]["state"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
