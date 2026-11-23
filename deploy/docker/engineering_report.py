"""Assemble actual Docker job artifacts and reject missing engineering proofs."""

import json
from pathlib import Path
import sys

from vibearr_runner.compact_evidence import (
    EVIDENCE,
    SERVICES,
    digest,
    validate_compact_report,
)


MAX_REPORT = 64 * 1024**2


def read(path):
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_REPORT:
        raise ValueError(
            "Select a bounded actual job artifact without links: " + path.name
        )
    return json.loads(path.read_bytes())


def observation(path, key, value):
    if path.is_symlink() or path.stat().st_size > MAX_REPORT:
        raise ValueError("Job output is linked or exceeds its fixed bound.")
    values = []
    for line in path.read_text().splitlines():
        if not line.startswith("{"):
            continue
        record = json.loads(line)
        if record.get(key) == value:
            values.append(record)
    if len(values) != 1:
        raise ValueError("Expected one actual observation in " + path.name)
    return values[0]


def lines(path):
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 1024**2:
        raise ValueError("Runtime observations are missing, linked or oversized.")
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def payloads(root):
    proofs = root / "engineering-proofs"
    restored = observation(root / "restore-database.log", "database_restored", True)
    backup = {
        "manifest": read(root / "backup-manifest.json"),
        "file_restore": observation(root / "restore-files.log", "files_restored", True),
        "database_restore": restored,
        "restored_tables": restored["restored_tables"],
        "business": observation(root / "restored-fixture.json", "action", "verify"),
        "runner": read(root / "restored-authority.json"),
        "worker": read(root / "restored-worker-authority.json"),
        "restored_capabilities": read(root / "restored-capabilities.json"),
        "restored_boundary": read(root / "restored-boundary.json"),
    }
    restart = {
        name.replace("-", "_"): read(root / (name + ".json"))
        for name in (
            "runner-before",
            "runner-after",
            "controller-before",
            "controller-after",
        )
    }
    restart["before"], restart["after"] = restart.pop("runner_before"), restart.pop(
        "runner_after"
    )
    return {
        "trusted_validation": read(proofs / "trusted-validation.json"),
        "changed_dependency": read(root / "dependency-smoke-proof.json"),
        "browser": read(root / "candidate-browser/proof.json"),
        "resource_limits": read(proofs / "resource-limits.json"),
        "worker_restart": read(proofs / "worker-restart.json"),
        "runner_restart": restart,
        "backup_restore": backup,
        "cli_packaging": observation(
            root / "packaging.log", "kind", "actual-packaging-proof"
        )["payload"],
    }


def main():
    root = Path(sys.argv[1])
    source = read(root / "source.json")
    if source["kind"] != "implementation-source":
        raise ValueError(
            "Engineering qualification requires an exact Git candidate input."
        )
    inspection = read(root / "runtime-inspection.json")
    if inspection["errors"] or len(inspection["containers"]) != 4:
        raise ValueError("Actual four-service inspection did not pass.")
    containers = {
        item["service"]: item["container_id"] for item in inspection["containers"]
    }
    images = {item["service"]: item["image"] for item in inspection["containers"]}
    capabilities = read(root / "engineering-proofs/capabilities.json")
    worker = {
        key: capabilities[key]
        for key in (
            "binding_digest",
            "instance_id",
            "worker_boot",
            "image_digest",
            "qualification_digest",
        )
    }
    evidence = {}
    for name, payload in payloads(root).items():
        document = {
            "kind": "actual-" + name,
            "runtime": "compact-worker-v1",
            "source_revision": source["commit_sha"],
            "source_digest": source["source_digest"],
            "tree_sha": source["tree_sha"],
            "worker": worker,
            "images": images,
            "payload": payload,
        }
        evidence[name] = {
            "kind": "actual-" + name,
            "passed": True,
            "fixtures": False,
            "source_revision": source["commit_sha"],
            "receipts": [{"sha256": digest(document), "document": document}],
        }
    if set(evidence) != set(EVIDENCE):
        raise ValueError("Engineering proof collection is incomplete.")
    report = {
        "schema_version": 1,
        "kind": "compact-stack-engineering",
        "runtime": "compact-worker-v1",
        "source_revision": source["commit_sha"],
        "source_digest": source["source_digest"],
        "tree_sha": source["tree_sha"],
        "passed": True,
        "fixtures": False,
        "services": list(SERVICES),
        "runtime_service_count": 4,
        "extra_runtime_services": [],
        "images": images,
        "containers": containers,
        "worker": worker,
        "worker_boundary": read(root / "engineering-proofs/worker-boundary.json"),
        "runtime_observations": {
            "idle": lines(root / "idle-resources.jsonl"),
            "phases": lines(root / "phase-counts.jsonl"),
            "worker_peak": read(root / "engineering-proofs/worker-boundary.json")[
                "resources"
            ],
        },
        "evidence": evidence,
        "release_pending": [
            "live subscription/provider renewal",
            "human merge acceptance",
        ],
    }
    validate_compact_report(report, capabilities)
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
