import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from .environment import load
from .initialize import write_once


class CredentialInitializationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "secret"
        self.chown = patch("deploy.docker.initialize.os.chown")
        self.chown.start()
        self.addCleanup(self.chown.stop)

    def test_existing_generated_credential_survives_restart(self):
        write_once(self.path, "")
        original = self.path.read_bytes()
        write_once(self.path, "")
        self.assertEqual(self.path.read_bytes(), original)

    def test_environment_cannot_silently_replace_saved_key(self):
        write_once(self.path, "original")
        with self.assertRaisesRegex(RuntimeError, "conflicts"):
            write_once(self.path, "replacement")
        self.assertEqual(self.path.read_text().strip(), "original")

    def test_world_readable_existing_credential_is_rejected(self):
        self.path.write_text("secret")
        self.path.chmod(0o644)
        with self.assertRaisesRegex(RuntimeError, "unsafe"):
            write_once(self.path, "")

    def test_loader_rejects_symlink_to_a_credential(self):
        directory = self.path.parent / "application"
        directory.mkdir()
        self.path.write_text("secret")
        self.path.chmod(0o600)
        (directory / "api-token").symlink_to(self.path)
        with self.assertRaises(OSError):
            load(self.path.parent)

    def test_loader_reads_declared_files_without_exporting_other_files(self):
        directory = self.path.parent / "application"
        directory.mkdir()
        path = directory / "api-token"
        path.write_text("container-token\n")
        path.chmod(0o600)
        with patch.dict(os.environ, {}, clear=True):
            load(self.path.parent)
            self.assertEqual(dict(os.environ), {"VIBEARR_API_TOKEN": "container-token"})
