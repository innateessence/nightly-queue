"""Portable image checks and separate, mandatory deployment boundary qualification."""

import argparse
import json
import io
import contextlib
import unittest
from pathlib import Path

import vibearr_runner

BOUNDARY_METHODS = {
    "test_actual_sandbox_stops_command_at_remaining_budget",
    "test_real_sandbox_validation_records_actual_exit_code",
}


def portable_suite():
    loader = unittest.TestLoader()
    suite = loader.discover(
        str(Path(vibearr_runner.__file__).parent),
        pattern="test_*.py",
        top_level_dir=str(Path(__file__).parent.parent),
    )
    for module in (
        "test_workspaces",
        "test_automatic_merge",
        "test_implementation",
        "test_delivery",
        "test_recovery",
        "test_claude_recovery",
        "test_opencode_recovery",
    ):
        path = Path(vibearr_runner.__file__).parent / "acceptance" / (module + ".py")
        if not path.exists():
            continue
        tests = loader.loadTestsFromName("vibearr_runner.acceptance." + module)

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
    if any(
        (args.executor_image, args.database_image, args.executor_state, args.profiles)
    ):
        parser.error(
            "Host executor flags are retired; qualify the Docker worker stack and configure its API client."
        )
    if args.boundary:
        from vibearr_executor.client import Client
        from vibearr_executor.contracts import canonical

        report = Client.from_environment().capabilities()
        workers = report.get("workers", {})
        if (
            report.get("runtime") != "compose-slot-v2"
            or report.get("protocol_version") != 2
            or report.get("qualification_version") != 1
            or report.get("qualified") is not True
            or set(workers) != {"agent", "setup", "validation"}
            or any(value.get("qualified") is not True for value in workers.values())
        ):
            raise RuntimeError(
                "Qualify every current Docker worker boot before verifying readiness."
            )
        if args.evidence:
            args.evidence.write_bytes(canonical(report) + b"\n")
        print(
            json.dumps(
                {
                    "runtime": "compose-slot-v2",
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
