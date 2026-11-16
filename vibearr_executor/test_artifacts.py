import io
import os
import tarfile
import tempfile
import unittest
from pathlib import Path

from .artifacts import extract, import_result, pack, result_manifest, snapshot, unpack
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

    def test_import_preserves_additions_deletions_and_modes(self):
        before = pack({"delete": (0o644, b"old"), "change": (0o644, b"old")})
        after = pack({"add": (0o755, b"new"), "change": (0o755, b"changed")})
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            extract(before, root)
            (root / ".git").mkdir()
            (root / ".git" / "config").write_text("trusted")
            import_result(root, before, after, result_manifest(before, after))
            self.assertFalse((root / "delete").exists())
            self.assertEqual((root / "change").read_bytes(), b"changed")
            self.assertEqual((root / "add").stat().st_mode & 0o777, 0o755)
            self.assertEqual((root / ".git" / "config").read_text(), "trusted")

    def test_import_rejects_digest_changes_before_writes(self):
        before = pack({"file": (0o644, b"old")})
        after = pack({"file": (0o644, b"new")})
        manifest = result_manifest(before, after)
        manifest["files"][0]["sha256"] = "0" * 64
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            extract(before, root)
            with self.assertRaises(Rejected):
                import_result(root, before, after, manifest)
            self.assertEqual((root / "file").read_bytes(), b"old")

    def test_import_rejects_preexisting_symlink_parent(self):
        before = pack({})
        after = pack({"linked/file": (0o644, b"new")})
        with (
            tempfile.TemporaryDirectory() as temporary,
            tempfile.TemporaryDirectory() as other,
        ):
            root = Path(temporary)
            (root / "linked").symlink_to(other, target_is_directory=True)
            with self.assertRaises(Rejected):
                import_result(root, before, after, result_manifest(before, after))
            self.assertFalse((Path(other) / "file").exists())

    def test_file_directory_transitions_are_preserved(self):
        cases = [
            ({"a": (0o644, b"old")}, {"a/b": (0o644, b"new")}),
            ({"a/b": (0o644, b"old")}, {"a": (0o644, b"new")}),
        ]
        for before_files, after_files in cases:
            before, after = pack(before_files), pack(after_files)
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                extract(before, root)
                import_result(root, before, after, result_manifest(before, after))
                self.assertEqual(unpack(snapshot(root)), after_files)

    def test_conflicting_untracked_file_is_preserved(self):
        before, after = pack({}), pack({"extra": (0o644, b"new")})
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "extra").write_text("unrelated")
            with self.assertRaises(Rejected):
                import_result(root, before, after, result_manifest(before, after))
            self.assertEqual((root / "extra").read_text(), "unrelated")
