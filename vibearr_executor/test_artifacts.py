import io
import os
import tarfile
import tempfile
import unittest
from pathlib import Path

from .artifacts import pack, snapshot, unpack
from .contracts import Rejected


def archive_member(name="file", kind=tarfile.REGTYPE, mode=0o644, link=""):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as archive:
        entry = tarfile.TarInfo(name)
        entry.type, entry.mode, entry.linkname = kind, mode, link
        archive.addfile(entry)
    return output.getvalue()


class ArchiveTests(unittest.TestCase):
    def test_long_unicode_paths_roundtrip_with_exact_bytes_and_modes(self):
        name = "nested/" + "é" * 100 + "/" + "file" * 40
        files = {name: (0o755, b"exact content\x00\xff")}
        self.assertEqual(unpack(pack(files)), files)

    def test_traversal_and_noncanonical_paths_are_rejected(self):
        for name in (
            "../escape",
            "/absolute",
            "a/../b",
            "a//b",
            "./file",
            ".git/config",
            "a/.git/hook",
            "a\\b",
        ):
            with self.subTest(name=name), self.assertRaises(Rejected):
                unpack(archive_member(name))

    def test_links_and_special_files_are_rejected(self):
        for kind in (
            tarfile.SYMTYPE,
            tarfile.LNKTYPE,
            tarfile.FIFOTYPE,
            tarfile.CHRTYPE,
            tarfile.BLKTYPE,
        ):
            with self.subTest(kind=kind), self.assertRaises(Rejected):
                unpack(archive_member(kind=kind, link="/etc/passwd"))

    def test_privileged_modes_are_rejected(self):
        with self.assertRaises(Rejected):
            unpack(archive_member(mode=0o4755))

    def test_duplicate_paths_are_rejected(self):
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode="w") as archive:
            archive.addfile(tarfile.TarInfo("file"))
            archive.addfile(tarfile.TarInfo("file"))
        with self.assertRaises(Rejected):
            unpack(output.getvalue())

    def test_file_cannot_be_an_ancestor(self):
        with self.assertRaises(Rejected):
            pack({"a": (0o644, b"a"), "a/b": (0o644, b"b")})

    def test_snapshot_rejects_external_symlinks(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "link").symlink_to("/etc/passwd")
            with self.assertRaises(OSError):
                snapshot(root)

    def test_snapshot_rejects_hardlinks(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "first").write_text("file")
            os.link(root / "first", root / "second")
            with self.assertRaises(Rejected):
                snapshot(root)
