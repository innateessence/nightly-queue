"""Image-owned offline distribution checks in the unprivileged tools task."""

import importlib.util
import os
from pathlib import Path
import subprocess
import unittest


def main():
    os.chdir("/work")
    os.mkdir("/work/.packaging-tmp", 0o700)
    os.environ["TMPDIR"] = "/work/.packaging-tmp"
    os.environ["SHELLCHECK_OPTS"] = "--norc"
    workflows = sorted(Path(".github/workflows").glob("*.yml"))
    if not workflows:
        raise RuntimeError("Candidate workflow coverage is missing.")
    subprocess.run(
        [
            "/usr/local/bin/actionlint",
            "-config-file",
            "/opt/validation/actionlint.yaml",
            *map(str, workflows),
        ],
        check=True,
    )
    scripts = [
        "test.sh",
        "run.sh",
        *map(str, sorted(Path("deploy/docker").glob("*.sh"))),
    ]
    subprocess.run(["/usr/bin/shellcheck", "--norc", *scripts], check=True)
    specification = importlib.util.spec_from_file_location(
        "distribution_checks", "/opt/validation/distribution_checks.py"
    )
    if specification is None or specification.loader is None:
        raise RuntimeError("Pinned packaging checks are missing.")
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    module.__file__ = "/work/vibearr/test_distributions.py"
    suite = unittest.defaultTestLoader.loadTestsFromModule(module)
    if suite.countTestCases() != 4:
        raise RuntimeError("Pinned packaging coverage changed.")
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if not result.wasSuccessful() or result.skipped:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
