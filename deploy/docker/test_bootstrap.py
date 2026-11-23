import fcntl
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from . import bootstrap
from .bootstrap import initialization_lock
from .initialize import write_once


class InitializationLockTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name) / "application"

    def test_duplicate_initialization_waits_on_the_same_volume_lock(self):
        with initialization_lock(self.directory):
            descriptor = os.open(self.directory / ".initialize.lock", os.O_WRONLY)
            with os.fdopen(descriptor, "w") as contender:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with initialization_lock(self.directory, timeout=0):
            pass

    def test_failed_initialization_releases_volume_lock_for_retry(self):
        with self.assertRaisesRegex(RuntimeError, "partial initialization"):
            with initialization_lock(self.directory):
                raise RuntimeError("partial initialization")
        with initialization_lock(self.directory, timeout=0):
            pass

    def test_lock_wait_has_a_deadline(self):
        with initialization_lock(self.directory):
            with self.assertRaisesRegex(RuntimeError, "timed out"):
                with initialization_lock(self.directory, timeout=0):
                    self.fail("Contender must not enter initialization")

    def test_symlink_cannot_choose_the_lock_file(self):
        self.directory.mkdir()
        target = self.directory.parent / "target"
        target.write_text("preserve me")
        (self.directory / ".initialize.lock").symlink_to(target)
        with self.assertRaises(OSError):
            with initialization_lock(self.directory):
                pass
        self.assertEqual(target.read_text(), "preserve me")

    def test_world_readable_lock_is_rejected(self):
        self.directory.mkdir()
        path = self.directory / ".initialize.lock"
        path.touch(mode=0o644)
        path.chmod(0o644)
        with self.assertRaisesRegex(RuntimeError, "permissions"):
            with initialization_lock(self.directory):
                pass

    def test_fifo_without_reader_cannot_block_initialization(self):
        self.directory.mkdir()
        os.mkfifo(self.directory / ".initialize.lock", mode=0o600)
        source = """
import errno
from pathlib import Path
import sys
from deploy.docker.bootstrap import initialization_lock

try:
    with initialization_lock(Path(sys.argv[1]), timeout=0):
        raise AssertionError("FIFO lock was accepted")
except OSError as error:
    if error.errno != errno.ENXIO:
        raise
"""
        subprocess.run(
            [sys.executable, "-c", source, str(self.directory)],
            check=True,
            capture_output=True,
            timeout=5,
        )

    def test_fifo_with_reader_is_rejected_as_nonregular(self):
        self.directory.mkdir()
        path = self.directory / ".initialize.lock"
        os.mkfifo(path, mode=0o600)
        reader = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        self.addCleanup(os.close, reader)
        with self.assertRaisesRegex(RuntimeError, "unsafe type"):
            with initialization_lock(self.directory, timeout=0):
                self.fail("FIFO lock was accepted")

    def test_hard_linked_lock_is_rejected(self):
        self.directory.mkdir()
        path = self.directory / ".initialize.lock"
        path.touch(mode=0o600)
        os.link(path, self.directory.parent / "linked")
        with self.assertRaisesRegex(RuntimeError, "links"):
            with initialization_lock(self.directory):
                pass


class BootstrapRetryTests(unittest.TestCase):
    def write(self, path, value, **kwargs):
        write_once(
            path,
            value,
            uid=os.getuid(),
            gid=os.getgid(),
            mode=kwargs.get("mode", 0o600),
        )

    def test_mounted_worker_tokens_are_independent_and_survive_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "secrets"
            worker = root / "worker"
            worker.mkdir(parents=True)
            original_mask = os.umask(0o077)
            try:
                with (
                    patch.dict(os.environ, {}, clear=True),
                    patch("deploy.docker.bootstrap.os.chown"),
                ):
                    with patch(
                        "deploy.docker.bootstrap.write_once", side_effect=self.write
                    ):
                        bootstrap.initialize(root, Path(directory) / "database")
                        first = (worker / "control-token").read_bytes()
                        second = (worker / "mediation-token").read_bytes()
                        bootstrap.initialize(root, Path(directory) / "database")
                self.assertGreaterEqual(len(first.strip()), 32)
                self.assertNotEqual(first, second)
                self.assertEqual((worker / "control-token").read_bytes(), first)
                self.assertEqual((worker / "mediation-token").read_bytes(), second)
                self.assertEqual(worker.stat().st_mode & 0o777, 0o750)
                self.assertEqual(
                    (worker / "control-token").stat().st_mode & 0o777, 0o440
                )
            finally:
                os.umask(original_mask)

    def test_retry_preserves_credentials_after_each_partial_startup_boundary(self):
        paths = [
            "database/password",
            "application/api-token",
            "application/encryption-key",
            "application/django-key",
        ]
        for boundary in paths:
            with (
                self.subTest(boundary=boundary),
                tempfile.TemporaryDirectory() as directory,
            ):
                root = Path(directory) / "secrets"
                initial_mask = os.umask(0o077)
                try:

                    def interrupted_write(path, value, **kwargs):
                        self.write(path, value, **kwargs)
                        if path == root / boundary:
                            raise OSError("startup was interrupted")

                    with (
                        patch.dict(os.environ, {}, clear=True),
                        patch("deploy.docker.bootstrap.os.chown"),
                    ):
                        with patch(
                            "deploy.docker.bootstrap.write_once",
                            side_effect=interrupted_write,
                        ):
                            with self.assertRaisesRegex(OSError, "interrupted"):
                                bootstrap.initialize(root, Path(directory) / "database")
                        saved = {
                            name: (root / name).read_bytes()
                            for name in paths
                            if (root / name).exists()
                        }
                        with patch(
                            "deploy.docker.bootstrap.write_once", side_effect=self.write
                        ):
                            bootstrap.initialize(root, Path(directory) / "database")
                        self.assertTrue(all((root / name).is_file() for name in paths))
                        for name, content in saved.items():
                            self.assertEqual((root / name).read_bytes(), content)
                finally:
                    os.umask(initial_mask)
