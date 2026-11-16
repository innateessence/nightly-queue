import hashlib
import json
import platform
import os
from pathlib import Path

from . import PROTOCOL_VERSION, QUALIFICATION_VERSION
from .contracts import canonical, digest
from .runtime import Runtime
from .engine import EngineError

REQUIRED_CHECKS = {
    "agent-boundary-egress-artifacts",
    "offline-validation",
    "resource-enforcement",
    "memory-exhaustion",
    "restart-and-descendant-stop",
    "retry-and-auth-contract",
    "archive-and-rebinding-rejection",
    "disposable-database-isolation",
    "credential-free-setup",
    "credential-and-receipt-isolation",
    "pid-exhaustion-stop",
}


def binding(runtime: Runtime) -> dict:
    version = json.loads(runtime.docker.run("version", "--format", "{{json .Server}}"))
    info = json.loads(runtime.docker.run("info", "--format", "{{json .}}"))
    source = hashlib.sha256()
    for path in sorted(Path(__file__).parent.glob("*.py")):
        if not path.name.startswith("test_"):
            source.update(path.name.encode() + b"\x00" + path.read_bytes())
    images = {}
    workers = {profile.image for profile in runtime.profiles.values()}
    databases = {
        profile.database_image
        for profile in runtime.profiles.values()
        if profile.database_image
    }
    for image in sorted(workers | databases):
        try:
            installed = json.loads(runtime.docker.run("image", "inspect", image))[0]
            contract = installed["Config"].get("Labels", {}) or {}
            present = installed["Id"] == image and (
                image not in workers
                or contract.get("io.vibearr.executor.protocol") == "1"
            )
        except EngineError:
            present = False
        images[image] = {"installed_contract": present}
    return {
        "protocol_version": PROTOCOL_VERSION,
        "qualification_version": QUALIFICATION_VERSION,
        "executor_source_digest": source.hexdigest(),
        "kernel": platform.release(),
        "docker_version": version["Version"],
        "docker_api_version": version["ApiVersion"],
        "engine_id": info["ID"],
        "storage_driver": info["Driver"],
        "cgroup_version": info["CgroupVersion"],
        "security_options": info["SecurityOptions"],
        "profiles": {
            key: profile.digest for key, profile in sorted(runtime.profiles.items())
        },
        "denied_ips": runtime.authorization_denied_ips(),
        "images": images,
        "max_active_runs": runtime.max_active_runs,
    }


def capabilities(runtime: Runtime) -> dict:
    current = binding(runtime)
    path = runtime.directory / "qualification.json"
    proof = json.loads(path.read_bytes()) if path.exists() else {}
    qualified = (
        canonical(proof.get("binding")) == canonical(current)
        and set(proof.get("checks", [])) == REQUIRED_CHECKS
        and all(
            set(proof.get("image_checks", {}).get(image, [])) == REQUIRED_CHECKS
            for image in {profile.image for profile in runtime.profiles.values()}
        )
        and all(image["installed_contract"] for image in current["images"].values())
    )
    storage = os.statvfs(runtime.directory)
    free_bytes = storage.f_bavail * storage.f_frsize
    artifact_bytes = sum(path.stat().st_size for path in runtime.artifacts.iterdir())
    journal_bytes = sum(
        path.stat().st_size for path in runtime.operations.rglob("*") if path.is_file()
    )
    capacity_ok = (
        free_bytes >= 128 * 1024 * 1024
        and artifact_bytes <= 512 * 1024 * 1024
        and journal_bytes <= 2 * 1024 * 1024 * 1024
    )
    return {
        "schema_version": 1,
        "protocol_version": PROTOCOL_VERSION,
        "qualification_version": QUALIFICATION_VERSION,
        "runtime": "docker-oci-v1",
        "qualified": qualified,
        "accepting_work": qualified and capacity_ok,
        "storage": {
            "free_bytes": free_bytes,
            "artifact_bytes": artifact_bytes,
            "journal_bytes": journal_bytes,
            "capacity_ok": capacity_ok,
        },
        "binding_digest": digest(canonical(current)),
        "profiles": [
            {
                "profile_id": profile.profile_id,
                "profile_digest": profile.digest,
                "image": profile.image,
                "phase": profile.phase,
                "version": profile.version,
                "limits": profile.data()["limits"],
                "network_hosts": list(profile.network_hosts),
                "backend_secret_env": profile.backend_secret_env,
                "database_image": profile.database_image,
            }
            for profile in runtime.profiles.values()
        ],
        "reason": (
            "Actual OCI boundary checks passed for this installed runtime."
            if qualified
            else "Run versioned OCI boundary qualification on this host; no production run is required."
        ),
        "native_agent_ready": False,
    }
