"""Image-owned clean interpreter entrypoint for candidate application tests."""

import os
from pathlib import Path
import sys
import unittest

WORK = Path("/work")
LEGACY_BOUNDARY_TESTS = {
    "test_actual_sandbox_stops_command_at_remaining_budget",
    "test_real_sandbox_validation_records_actual_exit_code",
}


def suite(kind: str) -> unittest.TestSuite:
    loader = unittest.TestLoader()
    if kind == "cli":
        result = loader.discover(str(WORK / "vibearr"), top_level_dir=str(WORK))
        result.addTests(loader.discover(str(WORK / "vibearr"), top_level_dir=str(WORK)))
        return result
    if kind == "runner":
        return loader.discover(str(WORK / "vibearr_runner"), top_level_dir=str(WORK))
    result = unittest.TestSuite()
    for path in sorted((WORK / "vibearr_runner" / "acceptance").glob("test_*.py")):
        tests = loader.loadTestsFromName("vibearr_runner.acceptance." + path.stem)

        def append(group):
            for test in group:
                if isinstance(test, unittest.TestSuite):
                    append(test)
                elif test._testMethodName not in LEGACY_BOUNDARY_TESTS:
                    result.addTest(test)

        append(tests)
    if result.countTestCases() == 0:
        raise RuntimeError("The required Git acceptance suite is missing.")
    return result


def main():
    check = sys.argv[1]
    os.chdir(WORK)
    os.umask(0o022)
    dependencies = Path("/opt/candidate/venv/lib/python3.12/site-packages")
    site = (
        dependencies
        if dependencies.exists()
        else Path("/usr/local/lib/python3.12/site-packages")
    )
    sys.path.insert(0, str(site))
    sys.path.insert(0, str(WORK))
    os.environ["PYTHONPATH"] = os.pathsep.join((str(site), str(WORK)))
    if check in {"migrations", "system", "django"}:
        from django.core.management import execute_from_command_line
        from django.test import LiveServerTestCase

        LiveServerTestCase.host = "127.0.0.1"

        arguments = {
            "migrations": ["makemigrations", "--check", "--dry-run"],
            "system": ["check"],
            "django": ["test", "--noinput"],
        }
        if check == "migrations":
            execute_from_command_line(["trusted-validation", "migrate", "--noinput"])
            execute_from_command_line(["trusted-validation", "migrate", "--check"])
        execute_from_command_line(["trusted-validation", *arguments[check]])
        return 0
    if check not in {"cli", "runner", "git-acceptance"}:
        raise RuntimeError("Choose a fixed application validation check.")
    result = unittest.TextTestRunner(verbosity=1).run(suite(check))
    if result.testsRun == 0:
        raise RuntimeError("Required validation suite discovered no tests.")
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
