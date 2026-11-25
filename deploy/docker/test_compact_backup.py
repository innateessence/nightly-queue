import json
import os
from pathlib import Path
import tempfile
import stat
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

from .backup import backup_volumes, connection
from .backup_files import VOLUMES, capture, inventory, restore
from .worker_recovery import recover, registered_channel


class CompactBackupTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source, self.target = self.root / "source", self.root / "target"
        for volume in VOLUMES:
            (self.source / volume).mkdir(parents=True)
            (self.target / volume).mkdir(parents=True)

    def test_compact_snapshot_preserves_worker_and_login_state(self):
        (self.source / "worker-control/instance").write_text(str(uuid4()))
        (self.source / "runner-logins/selected").write_text("private selected login")
        (self.source / "worker-secrets/control-token").write_text("a" * 64)
        archive = self.root / "state.tar"
        records = capture(self.source, archive, VOLUMES)
        restore(archive, records, self.target, VOLUMES)
        self.assertEqual(inventory(self.target, VOLUMES), records)
        self.assertEqual(
            (self.target / "runner-logins/selected").read_text(),
            "private selected login",
        )
        self.assertEqual({path.name for path in self.target.iterdir()}, set(VOLUMES))

    def test_unsupported_schema_is_rejected(self):
        manifest = {
            "schema_version": 2,
            "runtime": "compact-worker-v1",
            "state_volumes": list(VOLUMES),
        }
        with self.assertRaisesRegex(ValueError, "mapping"):
            backup_volumes(manifest)

    def test_compact_manifest_requires_exact_active_volume_mapping(self):
        manifest = {
            "schema_version": 3,
            "runtime": "compact-worker-v1",
            "state_volumes": list(VOLUMES),
        }
        self.assertEqual(backup_volumes(manifest), VOLUMES)
        manifest["state_volumes"].append("unregistered-volume")
        with self.assertRaisesRegex(ValueError, "mapping"):
            backup_volumes(manifest)

    def test_wrong_volume_archive_is_rejected_before_restore(self):
        unrelated = self.root / "unrelated"
        volumes = (*VOLUMES[:-1], "unregistered-control")
        for volume in volumes:
            (unrelated / volume).mkdir(parents=True)
        archive = self.root / "unrelated.tar"
        records = capture(unrelated, archive, volumes)
        with self.assertRaises(ValueError):
            restore(archive, records, self.target)
        self.assertEqual(list((self.target / "worker-control").iterdir()), [])


class BackupConnectionTests(unittest.TestCase):
    def test_fresh_connection_uses_vibearr_database_and_user(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            secrets = root / "database-secrets"
            secrets.mkdir()
            (secrets / "password").write_text("private database password")
            with (
                patch.dict(os.environ, {}, clear=True),
                patch("deploy.docker.backup.sql", return_value="170000"),
            ):
                connection(root)
                self.assertEqual(os.environ["PGDATABASE"], "vibearr")
                self.assertEqual(os.environ["PGUSER"], "vibearr")


class WorkerChannelModeTests(unittest.TestCase):
    def setUp(self):
        self.path = Path("/restore/worker-secrets/control-token")
        self.info = SimpleNamespace(
            st_mode=stat.S_IFREG | 0o440, st_uid=0, st_gid=10001
        )

    def test_registered_restored_channel_is_accepted(self):
        self.assertTrue(registered_channel(self.path, self.info))

    def test_arbitrary_group_readable_path_is_rejected(self):
        self.assertFalse(registered_channel(Path("/tmp/control-token"), self.info))

    def test_arbitrary_restored_filename_is_rejected(self):
        self.assertFalse(
            registered_channel(self.path.with_name("other-token"), self.info)
        )

    def test_task_owned_channel_is_rejected(self):
        self.info.st_uid = 1000
        self.assertFalse(registered_channel(self.path, self.info))

    def test_other_group_channel_is_rejected(self):
        self.info.st_gid = 1000
        self.assertFalse(registered_channel(self.path, self.info))

    def test_group_writable_channel_is_rejected(self):
        self.info.st_mode = stat.S_IFREG | 0o460
        self.assertFalse(registered_channel(self.path, self.info))


class WorkerAuthorityRestoreTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.secrets, self.control = self.root / "secrets", self.root / "control"
        self.secrets.mkdir(mode=0o700)
        self.control.mkdir(mode=0o700)
        self.identity = str(uuid4())
        self.marker = self.secrets / "restore-required.json"
        self.marker.write_text(
            json.dumps(
                {"backup_id": self.identity, "new_worker_identity_required": True}
            )
        )
        for name, value in (("control-token", "a" * 64), ("mediation-token", "b" * 64)):
            path = self.secrets / name
            path.write_text(value)
            path.chmod(0o600)
        (self.control / "instance").write_text("old worker instance")
        (self.control / "compact-qualification.json").write_text("old proof")
        self.root_identity = patch(
            "deploy.docker.worker_recovery.os.geteuid", return_value=0
        )
        self.root_identity.start()
        self.addCleanup(self.root_identity.stop)
        ownership = patch("deploy.docker.worker_recovery.os.chown")
        self.ownership = ownership.start()
        self.addCleanup(ownership.stop)

    def test_restore_retires_old_authority_and_preserves_archive(self):
        observed = recover(self.identity, self.secrets, self.control)
        self.assertTrue(observed["retired"])
        self.ownership.assert_called()
        for call in self.ownership.call_args_list:
            self.assertEqual(call.args[1:], (10001, 10001))
        self.assertFalse(self.marker.exists())
        self.assertNotEqual(
            (self.secrets / "control-token").read_text().strip(), "a" * 64
        )
        self.assertNotEqual(
            (self.secrets / "mediation-token").read_text().strip(), "b" * 64
        )
        archive = self.control / "restore-history" / self.identity
        self.assertEqual((archive / "instance").read_text(), "old worker instance")
        self.assertEqual(
            (archive / "compact-qualification.json").read_text(), "old proof"
        )
        self.assertEqual(
            {path.name for path in self.control.iterdir()}, {"restore-history"}
        )

    def test_completed_restore_replay_keeps_same_tokens(self):
        first = recover(self.identity, self.secrets, self.control)
        token = (self.secrets / "control-token").read_bytes()
        self.assertEqual(recover(self.identity, self.secrets, self.control), first)
        self.assertEqual((self.secrets / "control-token").read_bytes(), token)

    def test_mismatched_marker_preserves_tokens_and_control(self):
        self.marker.write_text(
            json.dumps(
                {"backup_id": str(uuid4()), "new_worker_identity_required": True}
            )
        )
        with self.assertRaisesRegex(ValueError, "marker"):
            recover(self.identity, self.secrets, self.control)
        self.assertEqual((self.secrets / "control-token").read_text(), "a" * 64)
        self.assertTrue((self.control / "instance").exists())

    def test_symlinked_control_cannot_be_archived(self):
        (self.control / "link").symlink_to(self.root / "outside")
        with self.assertRaisesRegex(ValueError, "links"):
            recover(self.identity, self.secrets, self.control)
        self.assertTrue(self.marker.exists())

    def test_restore_does_not_accept_changed_authority_on_retry(self):
        recover(self.identity, self.secrets, self.control)
        (self.secrets / "control-token").write_text("c" * 64)
        with self.assertRaisesRegex(ValueError, "changed"):
            recover(self.identity, self.secrets, self.control)
