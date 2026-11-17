import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from .environment import load
from .initialize import require_existing_credentials, write_once


class CredentialInitializationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "secret"

    def write(self, value):
        write_once(self.path, value, uid=os.getuid(), gid=os.getgid())

    def test_existing_generated_credential_survives_restart(self):
        self.write("")
        original = self.path.read_bytes()
        self.write("")
        self.assertEqual(self.path.read_bytes(), original)

    def test_environment_cannot_silently_replace_saved_key(self):
        self.write("original")
        with self.assertRaisesRegex(RuntimeError, "conflicts"):
            self.write("replacement")
        self.assertEqual(self.path.read_text().strip(), "original")

    def test_world_readable_existing_credential_is_rejected(self):
        self.path.write_text("secret")
        self.path.chmod(0o644)
        with self.assertRaisesRegex(RuntimeError, "unsafe"):
            self.write("")

    def test_existing_database_cannot_receive_implicit_replacement_credentials(self):
        database = self.path.parent / "database"
        database.mkdir()
        (database / "PG_VERSION").write_text("17")
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "original credentials"):
                require_existing_credentials(self.path.parent, database)

    def test_failed_credential_publication_leaves_no_partial_secret(self):
        with patch(
            "deploy.docker.initialize.os.fsync", side_effect=OSError("disk failure")
        ):
            with self.assertRaises(OSError):
                self.write("new private value")
        self.assertFalse(self.path.exists())
        self.assertEqual(list(self.path.parent.iterdir()), [])

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
