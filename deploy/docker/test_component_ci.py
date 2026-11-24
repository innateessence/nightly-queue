from copy import deepcopy
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from . import component_ci as ci


@unittest.skipUnless(
    shutil.which("git"), "Git source-assembly probes run in the Docker tools image"
)
class ComponentSourceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.git = ci.Git(self.source)
        self.git.run("init", "--quiet")
        for name in (
            "shared.py",
            "deleted.py",
            "executable.sh",
            "removed-by-dependency.py",
        ):
            (self.source / name).write_text("baseline\n")
        self.base = self.commit("baseline")
        (self.source / "shared.py").write_text("dependency\n")
        (self.source / "dependency.py").write_text("new dependency\n")
        (self.source / "removed-by-dependency.py").unlink()
        self.dependency = self.commit("dependency")
        self.git.run("checkout", "--quiet", "--detach", self.base)
        (self.source / "owned.py").write_text("actual PR code\n")
        self.candidate = self.commit("candidate")
        for name, value in {
            "BASE_SHA": self.base,
            "BASE_TREE": self.tree_sha(self.base),
            "DEPENDENCY_SHA": self.dependency,
            "DEPENDENCY_TREE": self.tree_sha(self.dependency),
        }.items():
            selected = patch.object(ci, name, value)
            selected.start()
            self.addCleanup(selected.stop)

    def commit(self, message):
        self.git.run("add", "--all")
        self.git.run(
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "--quiet",
            "-m",
            message,
        )
        return self.git.run("rev-parse", "HEAD").decode().strip()

    def tree_sha(self, revision):
        return self.git.run("rev-parse", revision + "^{tree}").decode().strip()

    def object_repository(self):
        output = self.root / ("derived-" + str(len(list(self.root.glob("derived-*")))))
        output.mkdir()
        git = ci.Git(output)
        git.run("init", "--quiet")
        shutil.copytree(
            self.source / ".git/objects", output / ".git/objects", dirs_exist_ok=True
        )
        return git

    def assemble(self, candidate=None, manifest=None):
        selected = candidate or self.candidate
        manifest = manifest or ci.generate_manifest(self.git, selected)
        git = self.object_repository()
        report = ci.assemble(git, selected, manifest)
        return git, report

    def test_candidate_and_complete_dependency_delta_are_combined(self):
        git, report = self.assemble()
        self.assertEqual((git.directory / "owned.py").read_text(), "actual PR code\n")
        self.assertEqual((git.directory / "shared.py").read_text(), "dependency\n")
        self.assertFalse((git.directory / "removed-by-dependency.py").exists())
        self.assertEqual(
            git.run("rev-list", "--parents", "-n", "1", "HEAD").decode().split()[1:],
            [self.candidate, self.dependency],
        )
        self.assertEqual(
            report["derived_tree"], git.run("rev-parse", "HEAD^{tree}").decode().strip()
        )
        self.assertTrue(report["entire_tree_verified"])

    def test_edit_to_dependency_path_is_preserved_as_actual_candidate_code(self):
        (self.source / "shared.py").write_text("PR canary must not be masked\n")
        candidate = self.commit("edit shared dependency")
        git, report = self.assemble(candidate)
        self.assertEqual(
            (git.directory / "shared.py").read_text(), "PR canary must not be masked\n"
        )
        entry = next(
            value for value in report["candidate_paths"] if value["path"] == "shared.py"
        )
        self.assertEqual(entry["candidate"], entry["derived"])

    def test_candidate_deletion_cannot_be_restored_by_dependency(self):
        (self.source / "deleted.py").unlink()
        candidate = self.commit("delete candidate file")
        git, report = self.assemble(candidate)
        self.assertNotIn("deleted.py", git.tree(report["derived_commit"]))
        self.assertFalse((git.directory / "deleted.py").exists())

    def test_executable_mode_only_candidate_change_is_preserved(self):
        (self.source / "executable.sh").chmod(0o755)
        candidate = self.commit("make executable")
        git, report = self.assemble(candidate)
        self.assertEqual(
            git.tree(report["derived_commit"])["executable.sh"]["mode"], "100755"
        )
        self.assertEqual(
            (git.directory / "executable.sh").stat().st_mode & 0o777, 0o755
        )

    def test_missing_dependency_entry_fails_closed(self):
        manifest = ci.generate_manifest(self.git, self.candidate)
        manifest["prerequisites"].pop()
        with self.assertRaisesRegex(ci.Rejected, "complete unowned"):
            self.assemble(manifest=manifest)

    def test_extra_unlisted_dependency_entry_fails_closed(self):
        manifest = ci.generate_manifest(self.git, self.candidate)
        manifest["prerequisites"].append(
            {"path": "unrelated.py", **self.git.tree(self.base)["deleted.py"]}
        )
        manifest["prerequisites"].sort(key=lambda value: value["path"])
        with self.assertRaisesRegex(ci.Rejected, "complete unowned"):
            self.assemble(manifest=manifest)

    def test_duplicate_dependency_entry_is_rejected(self):
        manifest = ci.generate_manifest(self.git, self.candidate)
        manifest["prerequisites"].append(deepcopy(manifest["prerequisites"][-1]))
        with self.assertRaisesRegex(ci.Rejected, "unique and sorted"):
            self.assemble(manifest=manifest)

    def test_tampered_dependency_blob_is_rejected(self):
        manifest = ci.generate_manifest(self.git, self.candidate)
        manifest["prerequisites"][0]["blob"] = "a" * 40
        with self.assertRaisesRegex(ci.Rejected, "complete unowned"):
            self.assemble(manifest=manifest)

    def test_dependency_cannot_change_candidate_mode(self):
        manifest = ci.generate_manifest(self.git, self.candidate)
        manifest["prerequisites"][0]["mode"] = "100755"
        with self.assertRaisesRegex(ci.Rejected, "complete unowned"):
            self.assemble(manifest=manifest)

    def test_stale_manifest_cannot_overwrite_a_new_candidate_edit(self):
        manifest = ci.generate_manifest(self.git, self.candidate)
        (self.source / "shared.py").write_text("later candidate change\n")
        candidate = self.commit("candidate updated after manifest")
        with self.assertRaisesRegex(ci.Rejected, "complete unowned"):
            self.assemble(candidate, manifest)

    def test_unlisted_index_mutation_is_detected_by_entire_tree_check(self):
        git = self.object_repository()
        original = git.run
        blob = self.git.tree(self.base)["deleted.py"]["blob"]

        def mutate(*arguments, **kwargs):
            if arguments == ("write-tree",):
                original(
                    "update-index", "--add", "--cacheinfo", f"100644,{blob},unlisted.py"
                )
            return original(*arguments, **kwargs)

        manifest = ci.generate_manifest(self.git, self.candidate)
        with patch.object(git, "run", side_effect=mutate):
            with self.assertRaisesRegex(ci.Rejected, "undeclared dependency"):
                ci.assemble(git, self.candidate, manifest)

    def test_control_input_must_match_the_exact_candidate_blob(self):
        with self.assertRaisesRegex(ci.Rejected, "exact candidate blob"):
            ci.verify_control_inputs(
                self.git.tree(self.candidate), {"owned.py": b"different source"}
            )

    def test_symlink_source_is_rejected_instead_of_followed(self):
        (self.source / "link.py").symlink_to("owned.py")
        candidate = self.commit("unsupported symlink")
        with self.assertRaisesRegex(ci.Rejected, "Symlinks"):
            ci.generate_manifest(self.git, candidate)

    def test_gitlink_source_is_rejected(self):
        self.git.run(
            "update-index", "--add", "--cacheinfo", f"160000,{self.base},submodule"
        )
        tree = self.git.run("write-tree").decode().strip()
        with self.assertRaisesRegex(ci.Rejected, "submodules"):
            self.git.tree(tree)

    def test_repeated_assembly_has_the_same_derived_commit(self):
        _, first = self.assemble()
        _, second = self.assemble()
        self.assertEqual(first["derived_commit"], second["derived_commit"])


class ManifestTests(unittest.TestCase):
    def test_manifest_source_is_the_unique_canonical_pr_path(self):
        self.assertEqual(
            ci.manifest_source(".github/component-prerequisites/pr-169.json"),
            ".github/component-prerequisites/pr-169.json",
        )

    def test_invalid_or_shared_manifest_paths_are_rejected(self):
        paths = (
            ".github/component-prerequisites.json",
            ".github/component-prerequisites/pr-0.json",
            ".github/component-prerequisites/pr-0169.json",
            ".github/component-prerequisites/pr-1000000000.json",
            ".github/component-prerequisites/pr--1.json",
            ".github/component-prerequisites/pr-169.json/extra",
            ".github/component-prerequisites/../pr-169.json",
            ".github/component-prerequisites/pr-169\n.json",
        )
        for path in paths:
            with self.subTest(path=path), self.assertRaises(ci.Rejected):
                ci.manifest_source(path)

    def test_another_pr_manifest_cannot_substitute_for_the_selected_pr(self):
        content = b"{}"
        candidate = {
            ".github/component-prerequisites/pr-170.json": {
                "mode": "100644",
                "type": "blob",
                "blob": ci.blob_identity(content),
            }
        }
        selected = ci.manifest_source(".github/component-prerequisites/pr-169.json")
        with self.assertRaisesRegex(ci.Rejected, "exact candidate blob"):
            ci.verify_control_inputs(candidate, {selected: content})

    @unittest.skipUnless(
        shutil.which("bash"), "Operator wrapper is checked in Docker tools"
    )
    def test_wrapper_rejects_bad_external_pr_numbers_before_docker_operations(self):
        script = Path(__file__).with_name("component-ci.sh")
        for number in ("0", "0169", "-1", "1000000000", "169/other", "169\n"):
            with self.subTest(number=number):
                result = subprocess.run(
                    ["bash", str(script)],
                    env={
                        "PATH": "/usr/local/bin:/usr/bin:/bin",
                        "CANDIDATE_SHA": "a" * 40,
                        "COMPONENT_PR_NUMBER": number,
                    },
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(result.returncode, 2)
                self.assertIn("canonical positive PR number", result.stderr)

    def test_duplicate_json_keys_are_rejected_before_schema_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.json"
            path.write_text('{"schema_version":1,"schema_version":1}')
            with self.assertRaisesRegex(ci.Rejected, "duplicate JSON"):
                ci.read_manifest(path)

    def test_manifest_byte_bound_is_enforced_before_json_parsing(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.json"
            path.write_bytes(b" " * (ci.MANIFEST_BYTES + 1))
            with self.assertRaisesRegex(ci.Rejected, "byte bound"):
                ci.read_manifest(path)

    def test_noncanonical_source_paths_are_rejected(self):
        for value in (
            "../escape",
            "a/../b",
            "/absolute",
            "a//b",
            ".git/config",
            "a\\b",
            "a\nb",
        ):
            with self.subTest(path=value), self.assertRaises(ci.Rejected):
                ci.source_path(value)

    def test_boolean_schema_version_is_not_an_integer_version(self):
        value = {
            "schema_version": True,
            "repository": ci.REPOSITORY,
            "base_sha": ci.BASE_SHA,
            "base_tree": ci.BASE_TREE,
            "dependency_sha": ci.DEPENDENCY_SHA,
            "dependency_tree": ci.DEPENDENCY_TREE,
            "prerequisites": [],
        }
        with self.assertRaisesRegex(ci.Rejected, "version"):
            ci.parse_manifest(value)
