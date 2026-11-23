import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from .backup import backup_volumes
from .backup_files import COMPACT_VOLUMES, VOLUMES, capture, inventory, restore
from .worker_recovery import recover


class CompactBackupTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source, self.target = self.root / "source", self.root / "target"
        for volume in COMPACT_VOLUMES:
            (self.source / volume).mkdir(parents=True)
            (self.target / volume).mkdir(parents=True)

    def test_compact_snapshot_preserves_worker_and_login_state(self):
        (self.source / "worker-control/instance").write_text(str(uuid4()))
        (self.source / "runner-logins/selected").write_text("private selected login")
        (self.source / "worker-secrets/control-token").write_text("a" * 64)
        archive = self.root / "state.tar"
        records = capture(self.source, archive, COMPACT_VOLUMES)
        restore(archive, records, self.target, COMPACT_VOLUMES)
        self.assertEqual(inventory(self.target, COMPACT_VOLUMES), records)
        self.assertEqual(
            (self.target / "runner-logins/selected").read_text(),
            "private selected login",
        )
        self.assertFalse((self.target / "runner-control").exists())

    def test_legacy_schema_is_not_relabelled_as_compact(self):
        legacy = {"schema_version": 2, "runtime": "integrated-runner-v1"}
        self.assertEqual(backup_volumes(legacy), VOLUMES)
        legacy.update(schema_version=3, runtime="compact-worker-v1")
        with self.assertRaisesRegex(ValueError, "mapping"):
            backup_volumes(legacy)

    def test_compact_manifest_requires_exact_active_volume_mapping(self):
        manifest = {
            "schema_version": 3,
            "runtime": "compact-worker-v1",
            "state_volumes": list(COMPACT_VOLUMES),
        }
        self.assertEqual(backup_volumes(manifest), COMPACT_VOLUMES)
        manifest["state_volumes"].append("runner-control")
        with self.assertRaisesRegex(ValueError, "mapping"):
            backup_volumes(manifest)

    def test_wrong_volume_archive_is_rejected_before_restore(self):
        legacy = self.root / "legacy"
        for volume in VOLUMES:
            (legacy / volume).mkdir(parents=True)
        archive = self.root / "legacy.tar"
        records = capture(legacy, archive)
        with self.assertRaises(ValueError):
            restore(archive, records, self.target, COMPACT_VOLUMES)
        self.assertEqual(list((self.target / "worker-control").iterdir()), [])


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

    def test_restore_retires_old_authority_and_preserves_archive(self):
        observed = recover(self.identity, self.secrets, self.control)
        self.assertTrue(observed["retired"])
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
