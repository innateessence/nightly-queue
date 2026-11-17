"""Container controller for the exact candidate, using only qualified slot APIs."""

import argparse
import json
import os
from pathlib import Path
from uuid import uuid4

from vibearr_executor.client import Client
from vibearr_executor.contracts import canonical
from vibearr_executor.validation_profile import ValidationProfile
from vibearr_runner.native_validation import CandidateSource
from vibearr_runner.validation_cli import run_candidate

from .git_input import ancestry, freeze_worktree, repository


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--commit")
    parser.add_argument("--base")
    parser.add_argument("--introduction")
    parser.add_argument("--expected-source-digest", required=True)
    options = parser.parse_args()
    exact = Path("/input/git-input").exists()
    if exact:
        checkout, selected = repository(
            Path("/input/git-input"), Path("/tmp/candidate.git"), options.commit
        )
        source = CandidateSource.from_git(checkout, selected)
    else:
        if any((options.commit, options.base, options.introduction)):
            raise ValueError(
                "Exact commit and ancestry checks require Git object input."
            )
        source = freeze_worktree(Path("/input/source"), Path("/tmp/candidate"))
        selected = source.commit_sha
    if source.digest != options.expected_source_digest:
        raise ValueError("Packaging and validation must use the same source archive.")
    if exact:
        ancestry(checkout, options.base, options.introduction)
    root = Path("/run/vibearr/runner")
    client = Client(
        "http://worker-gateway:8791",
        (root / "control-token").read_text().strip(),
        timeout=180,
    )
    profile = ValidationProfile.parse(
        json.loads((root / "validation-profile.json").read_bytes())
    )
    output = Path("/artifacts/candidate")
    output.mkdir(mode=0o700, parents=True)
    report = run_candidate(client, source, profile, state=output)
    (output / "evidence.json").write_bytes(canonical(report) + b"\n")
    from vibearr_runner.native_contract import validation_result

    if report["result"]["state"] == "passed":
        session = json.loads((output / "session.json").read_bytes())
        operations = [
            client.status(entry["manifest"]["operation_id"])
            for entry in session["operations"]
        ]
        native = validation_result(
            intent_id=str(uuid4()),
            session=session,
            operations=operations,
            provenance=report["dependency_provenance"],
            result=report["result"],
        )
        (output / "native-validation.json").write_bytes(canonical(native) + b"\n")
    print(
        json.dumps(
            {
                "kind": (
                    "exact-candidate-validation"
                    if exact
                    else "disposable-worktree-validation"
                ),
                "commit_sha": selected,
                "tree_sha": source.tree_sha,
                "source_digest": source.digest,
                "state": report["result"]["state"],
            }
        ),
        flush=True,
    )
    if report["result"]["state"] != "passed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
