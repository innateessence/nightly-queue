import os
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from .contracts import Rejected
from .control_token import read_control_token


class ControlTokenTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "control-token"
        self.path.write_text("a" * 48)
        self.path.chmod(0o600)

    def test_owner_only_regular_token_remains_supported(self):
        self.assertEqual(read_control_token(self.path), "a" * 48)

    def test_writable_group_token_is_rejected(self):
        self.path.chmod(0o660)
        with self.assertRaises(Rejected):
            read_control_token(self.path)

    def test_final_symlink_is_rejected(self):
        link = self.path.with_name("link")
        link.symlink_to(self.path)
        with self.assertRaisesRegex(Rejected, "links"):
            read_control_token(link)

    def test_parent_symlink_is_rejected(self):
        directory = self.path.parent.with_name(self.path.parent.name + "-link")
        directory.symlink_to(self.path.parent, target_is_directory=True)
        self.addCleanup(directory.unlink)
        with self.assertRaisesRegex(Rejected, "links"):
            read_control_token(directory / self.path.name)

    def test_hardlinked_token_is_rejected(self):
        os.link(self.path, self.path.with_name("other"))
        with self.assertRaisesRegex(Rejected, "regular"):
            read_control_token(self.path)

    def test_oversized_token_is_rejected(self):
        self.path.write_bytes(b"a" * 513)
        with self.assertRaisesRegex(Rejected, "bounded"):
            read_control_token(self.path)

    def test_non_ascii_token_is_rejected(self):
        self.path.write_text("é" * 48)
        with self.assertRaisesRegex(Rejected, "ASCII"):
            read_control_token(self.path)

    def test_multiline_token_is_rejected(self):
        self.path.write_text("a" * 32 + "\n" + "b" * 32)
        with self.assertRaisesRegex(Rejected, "malformed"):
            read_control_token(self.path)

    def shared_token(self, *, uid=0, gid=10001):
        return SimpleNamespace(
            st_mode=stat.S_IFREG | 0o440, st_uid=uid, st_gid=gid, st_nlink=1, st_size=48
        )

    def test_shared_root_group_token_requires_readonly_mount(self):
        with (
            patch("os.fstat", return_value=self.shared_token()),
            patch(
                "vibearr_executor.control_token.readonly_token_mount",
                return_value=False,
            ),
        ):
            with self.assertRaisesRegex(Rejected, "mount"):
                read_control_token(self.path)

    def test_shared_root_group_token_accepts_registered_readonly_mount(self):
        with (
            patch("os.fstat", return_value=self.shared_token()),
            patch(
                "vibearr_executor.control_token.readonly_token_mount", return_value=True
            ),
            patch("os.fstatvfs", return_value=SimpleNamespace(f_flag=os.ST_RDONLY)),
        ):
            self.assertEqual(read_control_token(self.path), "a" * 48)

    def test_shared_token_rejects_untrusted_owner(self):
        with (
            patch("os.fstat", return_value=self.shared_token(uid=1000)),
            patch(
                "vibearr_executor.control_token.readonly_token_mount", return_value=True
            ),
        ):
            with self.assertRaisesRegex(Rejected, "ownership"):
                read_control_token(self.path)

    def test_task_owned_private_token_is_rejected_even_by_root(self):
        info = SimpleNamespace(
            st_mode=stat.S_IFREG | 0o600,
            st_uid=1000,
            st_gid=1000,
            st_nlink=1,
            st_size=48,
        )
        with patch("os.fstat", return_value=info), patch("os.geteuid", return_value=0):
            with self.assertRaisesRegex(Rejected, "ownership"):
                read_control_token(self.path)
