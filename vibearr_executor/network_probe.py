"""Verify qualification stays valid during an ordinary registered operation."""

import argparse
import json
import time
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from .artifacts import pack, unpack
from .contracts import Limits, Profile, canonical, digest
from .engine import Docker
from .qualify import Harness, require, smoke_profiles
from .runtime import Runtime, durable


def registered_profile(image: str, database_image: str) -> Profile:
    recipe = (
        "import os,time; from pathlib import Path; "
        "from vibearr_executor.pg_smoke import query; "
        "query(os.environ['DATABASE_URL']); "
        "Path('/work/started.txt').write_text('ordinary database operation\\n'); "
        "print('ordinary-operation-started',flush=True); time.sleep(300)"
    )
    return Profile(
        "registered-network-validation-v1",
        1,
        image,
        "validation",
        ("python", "-I", "-c", recipe),
        Limits(),
        database_image=database_image,
    )


def probe(state: Path, profile: Profile, evidence: Path) -> dict:
    profiles = [
        Profile.parse(value)
        for value in json.loads((state / "profiles.json").read_bytes())
    ]
    require(
        profile in profiles, "Register this exact ordinary profile before qualification"
    )
    require(
        profile not in smoke_profiles(profile.image, profile.database_image),
        "Normal-operation proof must not bypass qualification as a smoke recipe",
    )
    harness = Harness(state, profiles)
    source = pack({"original.txt": (0o644, b"ordinary operation\n")})
    context = b"Trusted registered operation qualification stability probe."
    manifest: dict = {
        "schema_version": 1,
        "run_id": str(uuid4()),
        "operation_id": str(uuid4()),
        "profile_id": profile.profile_id,
        "profile_digest": profile.digest,
        "source_digest": digest(source),
        "context_digest": digest(context),
        "phase": profile.phase,
        "limits": asdict(profile.limits),
        "deadline": (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(),
        "network_hosts": [],
    }
    operation = manifest["operation_id"]
    observations: dict = {
        "operation_id": operation,
        "profile_digest": profile.digest,
        "stages": {},
    }

    def snapshot(stage: str) -> dict:
        capabilities = harness.client.capabilities()
        current = {
            "qualified": capabilities["qualified"],
            "binding_digest": capabilities["binding_digest"],
            "host_addresses": sorted(Runtime.host_addresses()),
        }
        observations["stages"][stage] = current
        durable(evidence, canonical(observations))
        require(
            current["qualified"],
            f"Ordinary operation revoked qualification at {stage}: {current}",
        )
        return current

    harness.start()
    try:
        baseline = snapshot("before")
        harness.client.upload("source", source)
        harness.client.upload("context", context)
        harness.client.prepare(manifest)
        first = harness.client.start(operation)
        require(first["state"] == "running", "Ordinary operation did not launch")
        record = json.loads(
            (state / "operations" / operation / "record.json").read_bytes()
        )
        docker = Docker()
        database = docker.inspect(record["name"] + "-db")
        require(
            database is not None and database["State"]["Running"],
            "Disposable database is not running",
        )
        assert database is not None
        require(
            snapshot("active")["binding_digest"] == baseline["binding_digest"],
            "Active network changed binding",
        )
        deadline = time.monotonic() + 30
        while b"ordinary-operation-started" not in harness.client.logs(operation):
            require(
                time.monotonic() < deadline, "Ordinary database recipe did not start"
            )
            require(
                harness.client.status(operation)["state"] == "running",
                "Ordinary recipe exited early",
            )
            time.sleep(0.2)
        harness.shutdown()
        harness.start()
        require(
            snapshot("restarted")["binding_digest"] == baseline["binding_digest"],
            "Restart changed binding",
        )
        recovered = harness.client.start(operation)
        require(
            recovered["container_id"] == first["container_id"],
            "Restart replaced the ordinary task",
        )
        stopped = harness.client.stop(operation)
        require(
            stopped.get("descendants_terminated") and stopped["state"] == "exited",
            "Cancel did not confirm termination",
        )
        require(
            stopped["container_id"] == first["container_id"],
            "Cancel replaced task identity",
        )
        require(
            docker.inspect(first["container_id"]) is None
            and docker.inspect(database["Id"]) is None,
            "Canceled containers remain",
        )
        require(
            snapshot("stopped")["binding_digest"] == baseline["binding_digest"],
            "Network cleanup changed binding",
        )
        inventory, exported = harness.client.export(operation, source)
        require(
            "started.txt" in unpack(exported), "Ordinary operation workspace was lost"
        )
        observations.update(
            container_id=first["container_id"],
            database_id=database["Id"],
            artifact=inventory,
            descendants_terminated=True,
        )
        durable(evidence, canonical(observations))
        return observations
    finally:
        try:
            harness.client.stop(operation)
        finally:
            harness.shutdown()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--database-image", required=True)
    parser.add_argument("--profiles-output", type=Path)
    parser.add_argument("--state", type=Path)
    parser.add_argument("--evidence", type=Path)
    args = parser.parse_args()
    profile = registered_profile(args.image, args.database_image)
    Profile.parse(profile.data())
    if args.profiles_output:
        args.profiles_output.write_bytes(canonical([profile.data()]))
        return
    if not args.state or not args.evidence:
        parser.error("--state and --evidence are required to probe a qualified runtime")
    print(json.dumps(probe(args.state, profile, args.evidence), sort_keys=True))


if __name__ == "__main__":
    main()
