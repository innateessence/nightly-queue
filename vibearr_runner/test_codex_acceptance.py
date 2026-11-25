import contextlib
import importlib.util
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from vibearr_runner import codex_acceptance


class NativeFixtureCommandTests(unittest.TestCase):
    def test_missing_execute_flag_cannot_read_a_credential_or_start_acceptance(self):
        with tempfile.TemporaryDirectory() as temporary:
            args = [
                "acceptance",
                "--state",
                temporary + "/state",
                "--credential-file",
                temporary + "/secret",
                "--model",
                "selected",
                "--evidence",
                temporary + "/evidence.json",
            ]
            with (
                patch("sys.argv", args),
                patch.object(codex_acceptance, "private_secret") as secret,
                patch.object(codex_acceptance, "acceptance") as execute,
            ):
                with (
                    contextlib.redirect_stderr(io.StringIO()),
                    self.assertRaises(SystemExit) as result,
                ):
                    codex_acceptance.main()
            self.assertEqual(result.exception.code, 2)
            secret.assert_not_called()
            execute.assert_not_called()

    def test_inspection_cannot_be_combined_with_new_work(self):
        with (
            patch(
                "sys.argv", ["acceptance", "--inspect-session", "/unused", "--execute"]
            ),
            patch.object(codex_acceptance, "inspect_session") as inspect,
        ):
            with (
                contextlib.redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit),
            ):
                codex_acceptance.main()
        inspect.assert_not_called()

    def test_credential_symlinks_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "secret").write_text("private key")
            (root / "linked").symlink_to(root / "secret")
            with self.assertRaises(OSError):
                codex_acceptance.private_secret(root / "linked")

    def test_public_credential_files_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "secret"
            path.write_text("private key")
            path.chmod(0o644)
            with self.assertRaisesRegex(RuntimeError, "private"):
                codex_acceptance.private_secret(path)

    def test_whitespace_is_not_a_credential(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "secret"
            path.write_text(" \n")
            path.chmod(0o600)
            with self.assertRaisesRegex(RuntimeError, "empty"):
                codex_acceptance.private_secret(path)


@unittest.skipUnless(
    importlib.util.find_spec("vibearr_executor"),
    "Install the native-runtime extra for fixture data checks.",
)
class NativeFixtureOracleTests(unittest.TestCase):
    def run_oracle(self, answer):
        from vibearr_executor.artifacts import extract, unpack, pack

        source = codex_acceptance.fixture(b"# reviewed baseline\n")
        files = unpack(source)
        files["answer.py"] = (0o644, f"def answer():\n    return {answer}\n".encode())
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            extract(pack(files), root)
            environment = {
                key: value for key, value in os.environ.items() if key != "PYTHONPATH"
            }
            return subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "unittest",
                    "discover",
                    "-s",
                    "vibearr_runner",
                    "-p",
                    "test_answer.py",
                ],
                cwd=root,
                env=environment,
                capture_output=True,
                check=False,
            )

    def test_independent_fixture_test_rejects_the_original_answer(self):
        self.assertNotEqual(self.run_oracle(0).returncode, 0)

    def test_independent_fixture_test_accepts_the_required_answer(self):
        self.assertEqual(self.run_oracle(42).returncode, 0)
