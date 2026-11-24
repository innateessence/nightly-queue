import fcntl
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from .runner_recovery import archive_connections, recover


class RunnerRestoreRootGateTests(unittest.TestCase):
    def test_nonroot_cannot_archive_connection_state(self):
        with patch("os.geteuid", return_value=10001):
            with self.assertRaisesRegex(ValueError, "trusted root"):
                recover(str(uuid4()), Path("/missing-secrets"), Path("/missing-state"))


@unittest.skipUnless(os.geteuid() == 0, "Requires the bounded root restore job")
class RunnerConnectionRestoreTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.secrets, self.state = self.root / "secrets", self.root / "state"
        for path in (self.secrets, self.state):
            path.mkdir(mode=0o700)
            os.chown(path, 10001, 10001)
        self.identity = str(uuid4())
        self.marker = self.secrets / "restore-required.json"
        self.marker.write_text(
            json.dumps(
                {"backup_id": self.identity, "new_runner_identity_required": True}
            )
        )
        lock = self.secrets / "bootstrap.lock"
        lock.touch(mode=0o600)
        self.marker.chmod(0o600)
        for path in (self.marker, lock):
            os.chown(path, 10001, 10001)
        self.connections = self.state / "connections"
        self.connections.mkdir(mode=0o700)
        self.ledger = self.connections / "manager.lock"
        self.ledger.write_bytes(b"Synthetic private connection ledger")
        self.ledger.chmod(0o600)
        self.archived = self.state / "restore-history" / self.identity / "connections"

    def restore(self):
        return recover(self.identity, self.secrets, self.state)

    def test_root_connection_archive_preserves_contents_owner_and_marker(self):
        marker = self.marker.read_bytes()
        result = self.restore()
        self.assertTrue(result["connections_archived"])
        self.assertFalse(self.connections.exists())
        ledger = self.archived / "manager.lock"
        self.assertEqual(ledger.read_bytes(), b"Synthetic private connection ledger")
        self.assertEqual((ledger.stat().st_uid, ledger.stat().st_gid), (0, 0))
        self.assertEqual(ledger.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.marker.read_bytes(), marker)
        for path in (self.archived.parent, self.archived.parent.parent):
            self.assertEqual((path.stat().st_uid, path.stat().st_gid), (10001, 10001))
            self.assertEqual(path.stat().st_mode & 0o777, 0o700)

    def test_replay_preserves_existing_archive(self):
        first = self.restore()
        self.assertEqual(self.restore(), first)
        self.assertEqual(
            (self.archived / "manager.lock").read_bytes(),
            b"Synthetic private connection ledger",
        )

    def test_different_marker_is_rejected_without_archiving(self):
        self.marker.write_text(
            json.dumps(
                {"backup_id": str(uuid4()), "new_runner_identity_required": True}
            )
        )
        with self.assertRaisesRegex(ValueError, "exact pending"):
            self.restore()
        self.assertTrue(self.ledger.is_file())
        self.assertFalse(self.archived.exists())

    def test_existing_connection_archive_is_never_overwritten(self):
        self.restore()
        self.connections.mkdir(mode=0o700)
        (self.connections / "second").write_text("preserve separately")
        with self.assertRaisesRegex(ValueError, "preserve both"):
            self.restore()
        self.assertTrue((self.archived / "manager.lock").is_file())
        self.assertEqual(
            (self.connections / "second").read_text(), "preserve separately"
        )

    def test_racing_empty_archive_is_never_overwritten(self):
        def race(source, target):
            self.archived.mkdir(mode=0o700)
            archive_connections(source, target)

        with patch(
            "deploy.docker.runner_recovery.archive_connections", side_effect=race
        ):
            with self.assertRaises(FileExistsError):
                self.restore()
        self.assertTrue(self.ledger.is_file())
        self.assertEqual(list(self.archived.iterdir()), [])

    def test_linked_connection_directory_is_rejected(self):
        self.connections.rename(self.state / "other")
        self.connections.symlink_to(self.state / "other", target_is_directory=True)
        with self.assertRaises(OSError):
            self.restore()
        self.assertTrue((self.state / "other/manager.lock").is_file())

    def test_linked_history_directory_is_rejected(self):
        destination = self.root / "outside"
        destination.mkdir(mode=0o700)
        (self.state / "restore-history").symlink_to(
            destination, target_is_directory=True
        )
        with self.assertRaises(OSError):
            self.restore()
        self.assertTrue(self.ledger.is_file())
        self.assertEqual(list(destination.iterdir()), [])

    def test_nonprivate_connection_state_is_rejected(self):
        self.connections.chmod(0o755)
        with self.assertRaisesRegex(ValueError, "ownership or mode"):
            self.restore()
        self.assertTrue(self.ledger.is_file())

    def test_marker_with_wrong_owner_is_rejected(self):
        os.chown(self.marker, 0, 0)
        with self.assertRaisesRegex(ValueError, "private bounded file"):
            self.restore()
        self.assertTrue(self.ledger.is_file())

    def test_marker_requires_a_boolean_retirement_request(self):
        self.marker.write_text(
            json.dumps({"backup_id": self.identity, "new_runner_identity_required": 1})
        )
        with self.assertRaisesRegex(ValueError, "exact pending"):
            self.restore()
        self.assertTrue(self.ledger.is_file())

    def test_linked_marker_is_rejected(self):
        saved = self.marker.with_name("saved-marker")
        self.marker.rename(saved)
        self.marker.symlink_to(saved)
        with self.assertRaises(OSError):
            self.restore()
        self.assertTrue(self.ledger.is_file())

    def test_existing_history_parent_owner_is_never_relaxed(self):
        (self.state / "restore-history").mkdir(mode=0o700)
        with self.assertRaisesRegex(ValueError, "ownership or mode"):
            self.restore()
        self.assertEqual((self.state / "restore-history").stat().st_uid, 0)
        self.assertTrue(self.ledger.is_file())

    def test_concurrent_bootstrap_fences_archival(self):
        with (self.secrets / "bootstrap.lock").open("rb") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(ValueError, "Another runner bootstrap"):
                self.restore()
        self.assertTrue(self.ledger.is_file())
