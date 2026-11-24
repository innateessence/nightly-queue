"""Capture the exact candidate through the registered model-free worker browser."""

import argparse
import base64
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sys
import time
from uuid import uuid4

from .client import Client
from .contracts import MAX_ARCHIVE, canonical, digest
from .control_token import read_control_token
from .runtime import durable


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--tree", required=True)
    parser.add_argument("--source-digest", required=True)
    parser.add_argument(
        "--state", type=Path, default=Path("/control/candidate-browser")
    )
    options = parser.parse_args()
    source = sys.stdin.buffer.read(MAX_ARCHIVE + 1)
    if len(source) > MAX_ARCHIVE or digest(source) != options.source_digest:
        raise ValueError("Browser candidate archive differs from its source binding.")
    token = read_control_token(Path(os.environ["VIBEARR_EXECUTOR_TOKEN_FILE"]))
    client = Client("http://127.0.0.1:8791", token, timeout=180)
    capabilities = client.capabilities()
    if capabilities.get("qualified") is not True:
        raise ValueError("Qualify the current worker before candidate browser capture.")
    profile = next(
        value
        for value in capabilities["profiles"]
        if value["profile_id"] == "browser-ui"
    )
    image = capabilities["image_digest"]
    binding = {
        "schema_version": 1,
        "revision": options.commit,
        "tree_sha": options.tree,
        "source_digest": options.source_digest,
        "application_image": image,
        "browser_image": image,
    }
    context = canonical({"browser_binding": binding})
    operation = str(uuid4())
    deadline = datetime.now(timezone.utc) + timedelta(seconds=180)
    manifest = {
        "schema_version": 1,
        "run_id": str(uuid4()),
        "operation_id": operation,
        "profile_id": profile["profile_id"],
        "profile_digest": profile["profile_digest"],
        "source_digest": digest(source),
        "context_digest": digest(context),
        "phase": "validation",
        "limits": profile["limits"],
        "network_hosts": [],
        "deadline": deadline.isoformat(),
    }
    options.state.mkdir(mode=0o700, parents=True, exist_ok=False)
    client.upload("source", source)
    client.upload("context", context)
    client.prepare(manifest)
    try:
        client.start(operation)
        while True:
            observed = client.status(operation)
            if observed["state"] == "exited":
                break
            if observed["state"] == "unknown" or datetime.now(timezone.utc) >= deadline:
                raise RuntimeError(
                    "Candidate browser phase did not terminate within its deadline."
                )
            time.sleep(0.25)
        report, archive = client.browser_result(operation)
        proof = {
            "report": report,
            "archive": base64.b64encode(archive).decode(),
            "archive_sha256": digest(archive),
            "operation": {**observed, "manifest": manifest},
        }
        durable(options.state / "proof.json", canonical(proof))
        durable(options.state / "capture.tar", archive)
        print(
            json.dumps(
                {
                    "kind": "actual-candidate-browser",
                    "source_revision": options.commit,
                    "operation_id": operation,
                    "archive_sha256": digest(archive),
                }
            )
        )
    finally:
        client.stop(operation)


if __name__ == "__main__":
    main()
