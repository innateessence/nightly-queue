import hashlib
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
import venv

BUILD_TOOLS = importlib.util.find_spec("setuptools") and importlib.util.find_spec(
    "wheel"
)


@unittest.skipUnless(
    BUILD_TOOLS, "Artifact tests run in the CI distribution-check environment"
)
class DistributionArtifactTests(unittest.TestCase):
    def run_command(self, arguments, directory):
        environment = dict(os.environ)
        environment.pop("PYTHONPATH", None)
        result = subprocess.run(
            arguments,
            cwd=directory,
            env=environment,
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout

    def check_artifact(self, source_name, runner):
        source = Path(__file__).resolve().parent.parent / source_name
        configuration = source / "pyproject.toml"
        if not configuration.exists():
            self.skipTest("Source build root is not included in the installed CLI")
        before = hashlib.sha256(configuration.read_bytes()).digest()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archives = root / "archives"
            archives.mkdir()
            command = "from setuptools.build_meta import build_sdist; import sys; build_sdist(sys.argv[1])"
            self.run_command([sys.executable, "-c", command, str(archives)], source)
            archive = next(archives.glob("*.tar.gz"))
            with tarfile.open(archive) as source_archive:
                names = source_archive.getnames()
                archived_configuration = next(
                    name for name in names if name.endswith("/pyproject.toml")
                )
                stream = source_archive.extractfile(archived_configuration)
                self.assertIsNotNone(stream)
                content = stream.read().decode() if stream else ""
                self.assertNotIn('"../coding_runner"', content)
                self.assertNotIn('"../vibearr_runner"', content)
            wheels = root / "wheels"
            arguments = [
                sys.executable,
                "-m",
                "pip",
                "wheel",
                "--no-build-isolation",
                "--no-deps",
                "--no-index",
            ]
            self.run_command(
                arguments + [str(archive), "--wheel-dir", str(wheels)], root
            )
            wheel = next(wheels.glob("*.whl"))
            environment = root / "environment"
            venv.EnvBuilder(with_pip=True).create(environment)
            executable = environment / "bin" / "python"
            self.run_command(
                [
                    str(executable),
                    "-m",
                    "pip",
                    "install",
                    "--no-index",
                    "--no-deps",
                    str(wheel),
                ],
                root,
            )
            if runner:
                canonical, legacy = "vibearr_runner.client", "coding_runner.client"
                commands = ["vibearr-runner", "coding-runner"]
            else:
                canonical, legacy = "vibearr.cli", "nq.cli"
                commands = ["vibearr", "nq"]
            check = "import importlib,sys; "
            check += f'assert importlib.import_module("{canonical}") is importlib.import_module("{legacy}"); '
            check += 'assert "django" not in sys.modules'
            self.run_command([str(executable), "-I", "-c", check], root)
            for command_name in commands:
                self.run_command(
                    [str(environment / "bin" / command_name), "--help"], root
                )
        self.assertEqual(hashlib.sha256(configuration.read_bytes()).digest(), before)

    def test_canonical_runner_sdist_installs_without_checkout(self):
        self.check_artifact("vibearr_runner", runner=True)

    def test_legacy_runner_sdist_installs_without_checkout(self):
        self.check_artifact("coding_runner", runner=True)

    def test_cli_sdist_installs_without_django(self):
        self.check_artifact(".", runner=False)
