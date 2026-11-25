"""Portable image checks and separate, mandatory deployment boundary qualification."""

import argparse
import json
import io
import contextlib
import unittest
from pathlib import Path

import vibearr_runner


def portable_suite():
    loader = unittest.TestLoader()
    suite = loader.discover(
        str(Path(vibearr_runner.__file__).parent),
        pattern="test_*.py",
        top_level_dir=str(Path(__file__).parent.parent),
    )
    for path in sorted(
        (Path(vibearr_runner.__file__).parent / "acceptance").glob("test_*.py")
    ):
        suite.addTest(
            loader.loadTestsFromName("vibearr_runner.acceptance." + path.stem)
        )
    return suite


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--boundary", action="store_true")
    parser.add_argument("--evidence", type=Path)
    args = parser.parse_args()
    if args.boundary:
        from vibearr_executor.client import Client
        from vibearr_executor.contracts import canonical

        report = Client.from_environment().capabilities()
        if (
            report.get("schema_version") != 1
            or report.get("runtime") != "compact-worker-v1"
            or report.get("protocol_version") != 2
            or report.get("qualification_version") != 1
            or report.get("qualified") is not True
        ):
            raise RuntimeError(
                "Qualify the current dedicated Docker worker boot before verifying readiness."
            )
        if args.evidence:
            args.evidence.write_bytes(canonical(report) + b"\n")
        print(
            json.dumps(
                {
                    "runtime": "compact-worker-v1",
                    "qualified": True,
                    "binding_digest": report["binding_digest"],
                    "native_agent_ready": False,
                },
                sort_keys=True,
            )
        )
        return 0
    suite = portable_suite()
    output = io.StringIO()
    with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
        result = unittest.TextTestRunner(stream=output, verbosity=2).run(suite)
    text = output.getvalue()
    print(text)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
