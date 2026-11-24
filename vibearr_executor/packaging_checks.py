"""Image-owned offline distribution checks in the unprivileged tools task."""

import importlib.util
import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess
import unittest


def main():
    os.chdir("/work")
    try:
        os.listdir("/input-source")
    except PermissionError:
        pass
    else:
        raise RuntimeError(
            "Packaging tasks must not read the controller's raw source input."
        )
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
    commands, wheels = [], {}
    original = module.DistributionArtifactTests.run_command

    def recorded_command(case, arguments, directory):
        output = original(case, arguments, directory)
        arguments = list(map(str, arguments))
        commands.append(
            {
                "argv": arguments,
                "exit_code": 0,
                "stdout_sha256": hashlib.sha256(output.encode()).hexdigest(),
            }
        )
        if "--wheel-dir" in arguments:
            wheel_root = Path(arguments[arguments.index("--wheel-dir") + 1])
            for path in wheel_root.glob("*.whl"):
                content = path.read_bytes()
                if len(content) > 8 * 1024**2:
                    raise RuntimeError(
                        "Distribution artifact exceeds its fixed evidence limit."
                    )
                sha = hashlib.sha256(content).hexdigest()
                wheels[sha] = {
                    "filename": path.name,
                    "sha256": sha,
                    "content": base64.b64encode(content).decode(),
                }
        return output

    module.DistributionArtifactTests.run_command = recorded_command
    suite = unittest.defaultTestLoader.loadTestsFromModule(module)
    if suite.countTestCases() != 4:
        raise RuntimeError("Pinned packaging coverage changed.")
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if not result.wasSuccessful() or result.skipped:
        raise SystemExit(1)
    proof = {
        "kind": "offline-packaging-artifacts",
        "commands": commands,
        "wheels": list(wheels.values()),
    }
    Path("/work/packaging-proof.json").write_text(json.dumps(proof, sort_keys=True))


if __name__ == "__main__":
    main()
