"""Real isolated validation cases; fixtures remain private and never publish work."""

from pathlib import Path
import subprocess

from .native_validation import CandidateSource
from .validation_cli import run_candidate

DATABASE_TEST = """import os
import unittest

import psycopg2


class ValidationDatabaseIsolationTests(unittest.TestCase):
    def test_validation_database_starts_empty(self):
        with psycopg2.connect(os.environ["DATABASE_URL"]) as connection:
            with connection.cursor() as cursor:
                cursor.execute("select to_regclass('public.vibearr_validation_marker')")
                self.assertIsNone(cursor.fetchone()[0])
                cursor.execute("create table vibearr_validation_marker (id integer)")
"""
FAILURE_TEST = """import unittest


class ValidationFailureTests(unittest.TestCase):
    def test_deliberate_failure_is_not_delivered(self):
        self.fail("Intentional validation acceptance failure")
"""
DEPENDENCY_TEST = """import unittest

import idna


class ValidationDependencyTests(unittest.TestCase):
    def test_candidate_dependency_is_installed(self):
        self.assertEqual(idna.__version__, "3.10")
"""


def fixture(source, directory: Path, changes: dict[str, bytes]):
    from vibearr_executor.artifacts import extract

    directory.mkdir(mode=0o700)
    extract(source.archive, directory)
    for name, content in changes.items():
        path = directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    arguments = [
        "git",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "commit.gpgsign=false",
        "-c",
        "user.name=Validation acceptance",
        "-c",
        "user.email=validation@example.invalid",
        "-C",
        str(directory),
    ]
    environment = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    for command in (
        ["init", "-q"],
        ["add", "-f", "."],
        ["commit", "-qm", "Isolated validation acceptance candidate"],
    ):
        subprocess.run(
            [*arguments, *command], env=environment, check=True, capture_output=True
        )
    commit = (
        subprocess.check_output([*arguments, "rev-parse", "HEAD"], env=environment)
        .decode()
        .strip()
    )
    return CandidateSource.from_git(directory, commit)


def acceptance(client, source, profile, state: Path):
    from vibearr_executor.artifacts import unpack

    passing = fixture(
        source,
        state / "passing-source",
        {"work_queue/test_validation_isolation.py": DATABASE_TEST.encode()},
    )
    cases = []
    exact_output = state / "exact-candidate"
    exact_output.mkdir(mode=0o700)
    exact = run_candidate(client, source, profile, state=exact_output)
    cases.append({"id": "exact-candidate", **exact})
    if exact["result"]["state"] != "passed":
        return {
            "schema_version": 1,
            "cases": cases,
            "result": {"state": "failed", "case": "exact-candidate"},
        }
    for name in ("passing", "repeated-fresh-database"):
        output = state / name
        output.mkdir(mode=0o700)
        result = run_candidate(client, passing, profile, state=output)
        cases.append({"id": name, **result})
        if result["result"]["state"] != "passed":
            return {
                "schema_version": 1,
                "cases": cases,
                "result": {"state": "failed", "case": name},
            }
    failing = fixture(
        source,
        state / "failing-source",
        {"work_queue/test_validation_failure.py": FAILURE_TEST.encode()},
    )
    ignored = fixture(
        source,
        state / "ignored-source",
        {
            ".gitignore": b"*\n",
            "deliberately_broken.py": b"def malformed(:\n",
            "test.sh": b"#!/bin/sh\nexit 0\n",
            "vibearr_executor/validation_task.py": b"raise SystemExit(0)\n",
        },
    )
    typed = fixture(
        source,
        state / "plugin-origin-source",
        {
            "mypy_django_plugin/__init__.py": b"",
            "mypy_django_plugin/main.py": b"raise SystemExit(0)\n",
            "validation_type_error.py": b'value: int = "not an integer"\n',
        },
    )
    for name, candidate, expected_check in (
        ("deliberate-test-failure", failing, "django"),
        ("ignore-and-recipe-bypass-rejected", ignored, "black"),
        ("trusted-plugin-detects-type-error", typed, "mypy"),
    ):
        output = state / name
        output.mkdir(mode=0o700)
        result = run_candidate(client, candidate, profile, state=output)
        cases.append({"id": name, **result})
        if (
            result["result"]["state"] != "failed"
            or result["result"]["check_id"] != expected_check
        ):
            return {
                "schema_version": 1,
                "cases": cases,
                "result": {"state": "failed", "case": name},
            }
    requirements = unpack(source.archive)["requirements.txt"][1]
    dependent = fixture(
        source,
        state / "dependency-source",
        {
            "requirements.txt": requirements + b"\nidna==3.10\n",
            "work_queue/test_validation_dependency.py": DEPENDENCY_TEST.encode(),
        },
    )
    output = state / "candidate-dependency-image"
    output.mkdir(mode=0o700)
    result = run_candidate(client, dependent, profile, state=output)
    cases.append({"id": "candidate-dependency-image", **result})
    complete = result["result"]["state"] == "passed" and bool(
        result["dependency_provenance"]
    )
    return {
        "schema_version": 1,
        "cases": cases,
        "result": {"state": "passed" if complete else "failed"},
    }
