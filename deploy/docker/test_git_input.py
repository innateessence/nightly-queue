from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from vibearr_executor.artifacts import unpack
from vibearr_runner.native_validation import CandidateSource

from .git_input import ancestry, freeze_worktree, repository, revision, validate


class ExactGitInputTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.checkout = self.root / "checkout"
        self.checkout.mkdir()
        self.git("init", "-q")
        self.git("config", "user.name", "Docker check")
        self.git("config", "user.email", "check@invalid")
        (self.checkout / "example.txt").write_text("exact committed bytes")
        self.git("add", ".")
        self.git("commit", "-qm", "fixture")
        self.commit = self.git("rev-parse", "HEAD")
        self.input = self.root / "input"
        self.input.mkdir()
        for name in ("objects", "refs"):
            shutil.copytree(self.checkout / ".git" / name, self.input / name)
        shutil.copyfile(self.checkout / ".git/HEAD", self.input / "HEAD")
        (self.input / "packed-refs").write_text("")

    def git(self, *arguments):
        return (
            subprocess.check_output(
                ["git", "-C", str(self.checkout), *arguments], stderr=subprocess.DEVNULL
            )
            .decode()
            .strip()
        )

    def test_sanitized_objects_preserve_the_exact_candidate_tree(self):
        path, commit = repository(self.input, self.root / "controlled.git")
        source = CandidateSource.from_git(path, commit)
        self.assertEqual(commit, self.commit)
        self.assertEqual(source.tree_sha, self.git("rev-parse", "HEAD^{tree}"))
        self.assertEqual(
            unpack(source.archive)["example.txt"][1], b"exact committed bytes"
        )

    def test_checkout_configuration_cannot_be_imported(self):
        (self.input / "config").write_text("[credential]\n helper = untrusted\n")
        with self.assertRaisesRegex(ValueError, "only objects"):
            validate(self.input)

    def test_linked_object_paths_are_rejected_without_reading_the_target(self):
        (self.input / "objects/linked").symlink_to(self.root / "private-sentinel")
        with self.assertRaisesRegex(ValueError, "links"):
            validate(self.input)

    def test_external_object_stores_cannot_supply_candidate_content(self):
        info = self.input / "objects/info"
        info.mkdir(exist_ok=True)
        (info / "alternates").write_text("/private/objects")
        with self.assertRaisesRegex(ValueError, "another object store"):
            validate(self.input)

    def test_packed_reference_can_select_the_exact_head(self):
        head = (self.input / "HEAD").read_text().strip()[5:]
        shutil.rmtree(self.input / "refs")
        (self.input / "refs").mkdir()
        (self.input / "packed-refs").write_text(self.commit + " " + head + "\n")
        self.assertEqual(revision(self.input), self.commit)

    def test_bootstrap_recipe_must_descend_from_the_selected_base(self):
        (self.checkout / "later.txt").write_text("later")
        self.git("add", ".")
        self.git("commit", "-qm", "later")
        later = self.git("rev-parse", "HEAD")
        ancestry(self.checkout, self.commit, later)
        with self.assertRaises(subprocess.CalledProcessError):
            ancestry(self.checkout, later, self.commit)

    def test_worktree_and_committed_input_use_the_same_canonical_archive(self):
        frozen = freeze_worktree(self.checkout, self.root / "frozen")
        committed = CandidateSource.from_git(self.checkout, self.commit)
        self.assertEqual(frozen.digest, committed.digest)
