"""Portable image checks and separate, mandatory deployment boundary qualification."""

import argparse
import json
import io
import contextlib
import tempfile
import unittest
from pathlib import Path

import coding_runner

BOUNDARY_METHODS = {
    "test_actual_sandbox_stops_command_at_remaining_budget",
    "test_real_sandbox_validation_records_actual_exit_code",
}


def portable_suite():
    loader = unittest.TestLoader()
    suite = loader.discover(
        str(Path(coding_runner.__file__).parent),
        pattern="test_*.py",
        top_level_dir=str(Path(__file__).parent.parent),
    )
    for module in (
        "test_workspaces",
        "test_implementation",
        "test_delivery",
        "test_recovery",
        "test_claude_recovery",
        "test_opencode_recovery",
    ):
        path = Path(coding_runner.__file__).parent / "acceptance" / (module + ".py")
        if not path.exists():
            continue
        tests = loader.loadTestsFromName("coding_runner.acceptance." + module)

        def add(group):
            for test in group:
                if isinstance(test, unittest.TestSuite):
                    add(test)
                elif test._testMethodName not in BOUNDARY_METHODS:
                    suite.addTest(test)

        add(tests)
    return suite


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--boundary", action="store_true")
    parser.add_argument("--evidence", type=Path)
    parser.add_argument("--executor-image")
    parser.add_argument("--database-image")
    parser.add_argument("--executor-state", type=Path)
    parser.add_argument("--profiles", type=Path)
    args = parser.parse_args()
    if args.boundary:
        if not args.executor_image or not args.database_image:
            parser.error(
                "OCI boundary verification requires installed --executor-image and --database-image sha256 IDs on the executor host."
            )
        from vibearr_executor.contracts import Profile, canonical
        from vibearr_executor.qualify import qualify, smoke_profiles

        profiles = smoke_profiles(args.executor_image, args.database_image)
        if args.profiles:
            profiles.extend(
                Profile.parse(value) for value in json.loads(args.profiles.read_bytes())
            )
        with tempfile.TemporaryDirectory(prefix="vibearr-oci-") as temporary:
            report = qualify(args.executor_state or Path(temporary), profiles)
        if args.evidence:
            args.evidence.write_bytes(canonical(report) + b"\n")
        print(
            json.dumps(
                {
                    "runtime": "docker-oci-v1",
                    "qualified": True,
                    "checks": report["checks"],
                    "binding": report["binding"],
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
