import io
from pathlib import Path
import tarfile
import tempfile
import unittest

from .contracts import Rejected, digest
from .dependency_artifacts import capture, manifest, read_layer


class DependencyArtifactTests(unittest.TestCase):
    def test_regular_environment_has_verified_requirement_digest(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            (directory / "bin").mkdir()
            (directory / "bin/python").write_bytes(b"trusted test binary")
            (directory / "pyvenv.cfg").write_text(
                "include-system-site-packages = false\n"
            )
            (directory / "resolved.txt").write_text("idna==3.10\n")
            content = capture(directory, b"idna==3.10\n")
        self.assertEqual(
            manifest(content)["requirements_digest"], digest(b"idna==3.10\n")
        )
        self.assertIn("venv/bin/python", read_layer(content))

    def archive(self, name, kind=tarfile.REGTYPE):
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode="w") as archive:
            entry = tarfile.TarInfo(name)
            entry.type = kind
            entry.linkname = "/etc/passwd" if kind == tarfile.SYMTYPE else ""
            archive.addfile(entry)
        return output.getvalue()

    def test_layer_cannot_escape_candidate_prefix(self):
        with self.assertRaises(Rejected):
            read_layer(self.archive("venv/../../protected"))

    def test_layer_cannot_replace_trusted_runtime(self):
        with self.assertRaises(Rejected):
            read_layer(self.archive("opt/executor/vibearr_executor/worker.py"))

    def test_layer_rejects_symlinks(self):
        with self.assertRaises(Rejected):
            read_layer(self.archive("venv/linked", tarfile.SYMTYPE))

    def test_capture_rejects_linked_dependency_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            (directory / "lib").symlink_to("/tmp")
            with self.assertRaises(Rejected):
                capture(directory, b"idna\n")
