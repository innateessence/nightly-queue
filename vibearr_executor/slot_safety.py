"""Observe actual slot ceilings rather than accepting job claims."""

import hashlib
import json
import os
from pathlib import Path

from .contracts import canonical, digest


def binding(runtime):
    limits = next(iter(runtime.profiles.values())).limits
    memory = int(Path("/sys/fs/cgroup/memory.max").read_text())
    pids = int(Path("/sys/fs/cgroup/pids.max").read_text())
    quota, period = Path("/sys/fs/cgroup/cpu.max").read_text().split()
    if (
        memory != limits.memory_mb * 1024 * 1024
        or pids != limits.pids
        or int(quota) != int(period) * limits.cpu
    ):
        raise RuntimeError(
            "Compose resource ceilings differ from the registered worker slot."
        )
    filesystem = os.statvfs(runtime.work)
    if filesystem.f_blocks * filesystem.f_frsize != limits.workspace_mb * 1024 * 1024:
        raise RuntimeError("Workspace tmpfs capacity differs from the registered slot.")
    if not os.statvfs("/").f_flag & os.ST_RDONLY:
        raise RuntimeError("Worker image root must be read-only.")
    if Path("/var/run/docker.sock").exists() or any(
        name.startswith("DOCKER_") for name in os.environ
    ):
        raise RuntimeError("Workers must not receive engine access.")
    code = hashlib.sha256()
    for path in sorted(Path(__file__).parent.glob("*.py")):
        if not path.name.startswith("test_"):
            code.update(path.name.encode() + b"\0" + path.read_bytes())
    return {
        "protocol_version": 2,
        "runtime": "compose-slot-v2",
        "instance_id": runtime.instance,
        "worker_boot": runtime.boot,
        "source_digest": code.hexdigest(),
        "profiles": {
            name: profile.digest for name, profile in runtime.profiles.items()
        },
        "limits": limits.__dict__,
    }


def capabilities(runtime):
    current = binding(runtime)
    proof = runtime.directory / "qualification.json"
    report = json.loads(proof.read_bytes()) if proof.exists() else {}
    qualified = (
        report.get("binding_digest") == digest(canonical(current))
        and report.get("complete") is True
    )
    return {
        "schema_version": 1,
        "protocol_version": 2,
        "qualification_version": 1,
        "runtime": "compose-slot-v2",
        "instance_id": runtime.instance,
        "worker_boot": runtime.boot,
        "binding_digest": digest(canonical(current)),
        "qualified": qualified,
        "accepting_work": qualified and not runtime.fenced and not runtime.active,
        "profiles": [
            {**profile.data(), "profile_digest": profile.digest}
            for profile in runtime.profiles.values()
        ],
        "reason": runtime.fenced
        or (
            "Worker slot qualified for this boot."
            if qualified
            else "Docker-only boundary qualification has not completed for this worker boot."
        ),
    }
