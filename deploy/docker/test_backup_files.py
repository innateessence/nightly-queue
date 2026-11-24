import io
import os
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from .backup_files import (
    VOLUMES,
    capture,
    inventory,
    require_capacity,
    restore,
    verify_archive,
)


class BackupVolumeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source, self.target = self.root / "source", self.root / "target"
        for name in VOLUMES:
            (self.source / name).mkdir(parents=True)
            (self.target / name).mkdir(parents=True)
        self.secret = self.source / "application-secrets/encryption-key"
        self.secret.write_bytes(b"original private key")
        self.secret.chmod(0o600)
        self.archive = self.root / "state.tar"

    def test_restore_preserves_private_bytes_modes_and_ownership(self):
        expected = capture(self.source, self.archive)
        restore(self.archive, expected, self.target)
        self.assertEqual(inventory(self.target), expected)
        self.assertEqual(
            (self.target / "application-secrets/encryption-key").read_bytes(),
            b"original private key",
        )

    def test_broker_sticky_directory_permissions_survive_restoration(self):
        (self.source / "runner-control").chmod(0o1777)
        expected = capture(self.source, self.archive)
        restore(self.archive, expected, self.target)
        self.assertEqual(
            (self.target / "runner-control").stat().st_mode & 0o7777, 0o1777
        )

    def test_setuid_files_cannot_be_included_in_a_backup(self):
        self.secret.chmod(0o4600)
        with self.assertRaises(ValueError):
            capture(self.source, self.archive)

    def test_nonempty_restore_target_is_never_overwritten(self):
        expected = capture(self.source, self.archive)
        marker = self.target / "runner-data/keep"
        marker.write_bytes(b"existing state")
        with self.assertRaisesRegex(ValueError, "empty"):
            restore(self.archive, expected, self.target)
        self.assertEqual(marker.read_bytes(), b"existing state")
        self.assertFalse((self.target / "application-secrets/encryption-key").exists())

    def test_insufficient_restore_space_is_rejected_before_copying(self):
        expected = capture(self.source, self.archive)
        with patch(
            "deploy.docker.backup_files.shutil.disk_usage",
            return_value=SimpleNamespace(free=0),
        ):
            with self.assertRaisesRegex(ValueError, "1 GiB reserve"):
                require_capacity(self.target, expected, 1024)
        self.assertFalse((self.target / "application-secrets/encryption-key").exists())

    def test_missing_database_size_cannot_bypass_restore_admission(self):
        expected = capture(self.source, self.archive)
        with self.assertRaisesRegex(ValueError, "size estimate"):
            require_capacity(self.target, expected, None)

    def test_changed_payload_is_rejected_before_any_restoration(self):
        expected = capture(self.source, self.archive)
        original = self.archive.read_bytes()
        self.archive.write_bytes(
            original.replace(b"original private key", b"tampered private key")
        )
        with self.assertRaisesRegex(ValueError, "digest"):
            restore(self.archive, expected, self.target)
        self.assertFalse((self.target / "application-secrets/encryption-key").exists())

    def test_source_symlink_is_not_followed_into_a_backup(self):
        (self.source / "runner-data/link").symlink_to(self.secret)
        with self.assertRaisesRegex(ValueError, "without links"):
            capture(self.source, self.archive)

    def test_archive_cannot_escape_a_volume_root(self):
        expected = capture(self.source, self.archive)
        with tarfile.open(self.archive, "a") as archive:
            member = tarfile.TarInfo("runner-data/../../escape")
            member.size = 1
            archive.addfile(member, io.BytesIO(b"x"))
        with self.assertRaisesRegex(ValueError, "unsafe"):
            restore(self.archive, expected, self.target)
        self.assertFalse((self.target / "application-secrets/encryption-key").exists())

    def test_archive_links_are_rejected_before_extraction(self):
        expected = capture(self.source, self.archive)
        member = tarfile.TarInfo("runner-data/link")
        member.type, member.linkname = tarfile.SYMTYPE, "/tmp/escape"
        with tarfile.open(self.archive, "a") as archive:
            archive.addfile(member)
        with tarfile.open(self.archive, "r:") as archive:
            with self.assertRaises(ValueError):
                verify_archive(archive, expected)

    def test_backup_records_do_not_contain_private_file_contents(self):
        expected = capture(self.source, self.archive)
        self.assertNotIn("original private key", str(expected))
        value = next(
            entry
            for entry in expected
            if entry["path"] == "application-secrets/encryption-key"
        )
        self.assertEqual(value["uid"], os.geteuid())
