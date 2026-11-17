"""Export canonical candidate files for disposable implementation builds."""

import argparse
import json
from pathlib import Path
import sys

from vibearr_runner.native_validation import CandidateSource
from vibearr_executor.artifacts import unpack

from .git_input import ancestry, repository


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--base")
    parser.add_argument("--introduction")
    options = parser.parse_args()
    checkout, selected = repository(
        Path("/input/git-input"), Path("/tmp/export.git"), options.commit
    )
    ancestry(checkout, options.base, options.introduction)
    source = CandidateSource.from_git(checkout, selected)
    files = unpack(source.archive)
    for name in ("requirements.txt", "vibearr_executor/validation-requirements.txt"):
        if files.get(name, (None, None))[1] != (Path("/app") / name).read_bytes():
            raise ValueError(
                "Runtime implementation builds require reviewed dependency images; use candidate validation for dependency changes."
            )
    print(
        json.dumps(
            {
                "kind": "implementation-source",
                "commit_sha": selected,
                "tree_sha": source.tree_sha,
                "source_digest": source.digest,
            }
        ),
        file=sys.stderr,
    )
    sys.stdout.buffer.write(source.archive)


if __name__ == "__main__":
    main()
