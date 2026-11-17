"""Freeze exact Git objects or a disposable worktree snapshot inside Docker."""

import argparse
import json
from pathlib import Path
import sys

from vibearr_runner.native_validation import CandidateSource
from .git_input import ancestry, freeze_worktree, repository


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--commit")
    parser.add_argument("--base")
    parser.add_argument("--introduction")
    options = parser.parse_args()
    if Path("/input/git-input").exists():
        checkout, selected = repository(
            Path("/input/git-input"), Path("/tmp/input.git"), options.commit
        )
        ancestry(checkout, options.base, options.introduction)
        source = CandidateSource.from_git(checkout, selected)
        kind = "implementation-source"
    else:
        if options.commit or options.base or options.introduction:
            raise ValueError("Exact commit selection requires Git object input.")
        source = freeze_worktree(Path("/input/source"), Path("/tmp/frozen"))
        kind = "disposable-snapshot"
    print(
        json.dumps(
            {
                "kind": kind,
                "commit_sha": source.commit_sha,
                "tree_sha": source.tree_sha,
                "source_digest": source.digest,
            }
        ),
        file=sys.stderr,
    )
    sys.stdout.buffer.write(source.archive)


if __name__ == "__main__":
    main()
