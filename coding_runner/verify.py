"""Portable image checks and separate, mandatory deployment boundary qualification."""

import argparse
import json
import tempfile
import unittest
from pathlib import Path

from .sandbox import SandboxUnavailable, probe_sandbox

BOUNDARY_METHODS = {
    "test_actual_sandbox_stops_command_at_remaining_budget",
    "test_real_sandbox_validation_records_actual_exit_code",
}


def portable_suite():
    loader = unittest.TestLoader()
    suite = loader.discover(
        str(Path(__file__).parent),
        pattern="test_*.py",
        top_level_dir=str(Path(__file__).parent.parent),
    )
    for module in (
        "test_workspaces",
        "test_implementation",
        "test_delivery",
        "test_recovery",
    ):
        path = Path(__file__).parent / "acceptance" / (module + ".py")
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


def boundary_suite():
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromName("coding_runner.boundary_checks")
    for path in sorted((Path(__file__).parent / "acceptance").glob("test_*.py")):
        suite.addTests(
            loader.loadTestsFromName("coding_runner.acceptance." + path.stem)
        )
    return suite


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--boundary", action="store_true")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as temporary:
        try:
            probe_sandbox(Path(temporary))
        except SandboxUnavailable:
            print(
                json.dumps(
                    {
                        "sandbox": "unsupported",
                        "execution_ready": False,
                        "boundary_qualification": "blocked",
                    }
                )
            )
            if args.boundary:
                return 1
        else:
            print(
                json.dumps(
                    {
                        "sandbox": "available",
                        "boundary_qualification": "requires full suite and live scenarios",
                    }
                )
            )
    suite = boundary_suite() if args.boundary else portable_suite()
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
