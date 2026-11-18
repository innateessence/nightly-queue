"""Operator-invoked nine-check validation through the integrated local service."""

import argparse
import json
from pathlib import Path
import sys

from .client import Client
from .contracts import MAX_ARCHIVE, canonical
from .validation_profile import ValidationProfile
from vibearr_runner.native_validation import CandidateSource
from vibearr_runner.validation_cli import run_candidate


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--commit", required=True)
    parser.add_argument("--tree", required=True)
    parser.add_argument("--source-digest", required=True)
    parser.add_argument("--state", type=Path, default=Path("/control/candidate-check"))
    options = parser.parse_args()
    content = sys.stdin.buffer.read(MAX_ARCHIVE + 1)
    if len(content) > MAX_ARCHIVE:
        raise ValueError("Candidate exceeds its archive limit.")
    source = CandidateSource(options.commit, options.tree, content)
    if source.digest != options.source_digest:
        raise ValueError("Candidate archive differs from its source binding.")
    root = Path("/run/vibearr/runner")
    client = Client(
        "http://127.0.0.1:8791",
        (root / "control-token").read_text().strip(),
        timeout=180,
    )
    profile = ValidationProfile.parse(
        json.loads((root / "validation-profile.json").read_bytes())
    )
    options.state.mkdir(mode=0o700, parents=True, exist_ok=False)
    report = run_candidate(client, source, profile, state=options.state)
    (options.state / "evidence.json").write_bytes(canonical(report) + b"\n")
    print(
        json.dumps(
            {
                "commit_sha": source.commit_sha,
                "tree_sha": source.tree_sha,
                "source_digest": source.digest,
                "state": report["result"]["state"],
            }
        )
    )
    if report["result"]["state"] != "passed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
