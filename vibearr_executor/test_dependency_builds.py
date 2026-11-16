from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from .artifacts import pack
from .contracts import Conflict, Rejected, digest
from .dependency_artifacts import capture, manifest, read_layer
from .runtime import Runtime
from .engine import EngineError
from .validation_profile import ValidationProfile


class DependencyArtifactTests(unittest.TestCase):
    def test_regular_environment_has_verified_requirement_digest(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            (directory / "bin").mkdir()
            (directory / "bin/python").write_bytes(b"trusted test binary")
            (directory / "pyvenv.cfg").write_text(
                "include-system-site-packages = false\n"
            )
            (directory / "resolved.txt").write_text("idna==3.10\n")
            content = capture(directory, b"idna==3.10\n")
        self.assertEqual(
            manifest(content)["requirements_digest"], digest(b"idna==3.10\n")
        )
        self.assertIn("venv/bin/python", read_layer(content))

    def archive(self, name, kind=tarfile.REGTYPE):
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode="w") as archive:
            entry = tarfile.TarInfo(name)
            entry.type = kind
            entry.linkname = "/etc/passwd" if kind == tarfile.SYMTYPE else ""
            archive.addfile(entry)
        return output.getvalue()

    def test_layer_cannot_escape_candidate_prefix(self):
        with self.assertRaises(Rejected):
            read_layer(self.archive("venv/../../protected"))

    def test_layer_cannot_replace_trusted_runtime(self):
        with self.assertRaises(Rejected):
            read_layer(self.archive("opt/executor/vibearr_executor/worker.py"))

    def test_layer_rejects_symlinks(self):
        with self.assertRaises(Rejected):
            read_layer(self.archive("venv/linked", tarfile.SYMTYPE))

    def test_capture_rejects_linked_dependency_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            (directory / "lib").symlink_to("/tmp")
            with self.assertRaises(Rejected):
                capture(directory, b"idna\n")


class DependencyBuildContractTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.profile = ValidationProfile(
            "sha256:" + "a" * 64, "sha256:" + "b" * 64, digest(b"original\n")
        )
        self.runtime = Runtime(
            Path(self.temporary.name),
            self.profile.executor_profiles() + [self.profile.setup_profile()],
        )
        self.addCleanup(self.runtime.close)
        self.source = pack({"requirements.txt": (0o644, b"idna==3.10\n")})
        self.runtime.upload("source", self.source, digest(self.source))
        self.request = {
            "schema_version": 1,
            "run_id": str(uuid4()),
            "build_id": str(uuid4()),
            "source_digest": digest(self.source),
            "setup_profile_id": self.profile.setup_profile().profile_id,
            "validation_profile": self.profile.data(),
            "limits": asdict(self.profile.setup_profile().limits),
            "deadline": (
                datetime.now(timezone.utc) + timedelta(minutes=10)
            ).isoformat(),
            "network_hosts": ["pypi.org", "files.pythonhosted.org"],
        }

    def prepare(self):
        with (
            patch(
                "vibearr_executor.qualification.capabilities",
                return_value={"qualified": True},
            ),
            patch(
                "vibearr_executor.qualification.binding", return_value={"host": "test"}
            ),
        ):
            return self.runtime.dependency_builds.prepare(self.request)

    def test_build_identity_binds_all_inputs_and_survives_reopen(self):
        first = self.prepare()
        self.runtime.close()
        self.runtime = Runtime(
            Path(self.temporary.name),
            self.profile.executor_profiles() + [self.profile.setup_profile()],
        )
        self.addCleanup(self.runtime.close)
        self.assertEqual(self.runtime.dependency_builds.prepare(self.request), first)
        changed = {**self.request, "source_digest": "f" * 64}
        with self.assertRaises(Conflict):
            self.runtime.dependency_builds.prepare(changed)

    def test_build_cannot_supply_docker_arguments(self):
        with self.assertRaises(Rejected):
            self.runtime.dependency_builds.prepare({**self.request, "mounts": ["/"]})

    def test_unknown_build_retains_executor_fence(self):
        self.prepare()
        record = self.runtime.dependency_builds.read(self.request["build_id"])
        record["state"] = "unknown"
        self.runtime.dependency_builds.save(record)
        self.assertTrue(self.runtime.dependency_builds.blocked(str(uuid4())))
        self.assertEqual(
            self.runtime.dependency_builds.start(self.request["build_id"])["state"],
            "unknown",
        )

    def test_canceled_prepared_build_cannot_later_launch(self):
        self.prepare()
        with (
            patch.object(self.runtime, "owned", return_value=None),
            patch.object(self.runtime.docker, "inspect", return_value=None),
            patch.object(self.runtime.docker, "remove"),
        ):
            result = self.runtime.dependency_builds.stop(self.request["build_id"])
        self.assertEqual(result["state"], "canceled")
        with patch.object(self.runtime.docker, "run") as engine:
            self.assertEqual(
                self.runtime.dependency_builds.start(self.request["build_id"])["state"],
                "canceled",
            )
        engine.assert_not_called()

    def test_derived_profile_cannot_be_used_by_another_run(self):
        self.prepare()
        record = self.runtime.dependency_builds.read(self.request["build_id"])
        record.update(
            state="ready",
            profile=ValidationProfile(
                "sha256:" + "c" * 64,
                self.profile.database_image,
                digest(b"idna==3.10\n"),
            ).data(),
        )
        self.runtime.dependency_builds.save(record)
        with patch.object(
            self.runtime.dependency_builds, "qualified", return_value=True
        ):
            self.assertTrue(
                self.runtime.dependency_builds.profiles(run_id=self.request["run_id"])
            )
            self.assertEqual(
                self.runtime.dependency_builds.profiles(run_id=str(uuid4())), {}
            )

    def test_setup_cannot_expand_egress(self):
        self.request["network_hosts"] = ["unconfigured.example.com"]
        with self.assertRaises(Rejected):
            self.prepare()

    def assembly(self):
        self.prepare()
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            (directory / "bin").mkdir()
            (directory / "bin/python").write_bytes(b"test binary")
            (directory / "pyvenv.cfg").write_text(
                "include-system-site-packages = false\n"
            )
            (directory / "resolved.txt").write_text("idna==3.10\n")
            content = capture(directory, b"idna==3.10\n")
        (self.runtime.path(self.request["build_id"]) / "dependencies.tar").write_bytes(
            content
        )
        record = self.runtime.dependency_builds.read(self.request["build_id"])
        record.update(state="assembling", dependency_digest=digest(content))
        self.runtime.dependency_builds.save(record)
        return record

    def test_copy_only_assembly_never_starts_a_candidate_process(self):
        record = self.assembly()
        engine = AssemblyEngine(self.profile.image)
        self.runtime.docker = engine
        self.runtime.dependency_builds.assemble(record)
        self.assertEqual(record["state"], "qualifying")
        self.assertEqual(record["assembly_container_id"], "original-container")
        self.assertEqual([value[0] for value in engine.commands].count("commit"), 1)
        self.assertFalse(
            any(value[0] in {"start", "run", "build"} for value in engine.commands)
        )
        copy = next(value for value in engine.commands if value[0] == "cp")
        self.assertTrue(copy[2].endswith(":/opt/candidate/"))

    def test_replacement_assembly_container_is_not_adopted(self):
        record = self.assembly()
        record["assembly_container_id"] = "original-container"
        self.runtime.dependency_builds.save(record)
        engine = AssemblyEngine(self.profile.image)
        engine.current = {
            "Id": "replacement",
            "Image": self.profile.image,
            "State": {"Status": "created"},
            "Config": {
                "Labels": self.runtime.dependency_builds.assembly_labels(record)
            },
        }
        self.runtime.docker = engine
        with self.assertRaises(EngineError):
            self.runtime.dependency_builds.assemble(record)
        self.assertFalse(any(value[0] in {"cp", "commit"} for value in engine.commands))

    def test_lost_commit_response_reconciles_without_committing_again(self):
        record = self.assembly()
        engine = AssemblyEngine(self.profile.image, lose_response=True)
        self.runtime.docker = engine
        with self.assertRaises(EngineError):
            self.runtime.dependency_builds.assemble(record)
        record["state"] = "unknown"
        self.runtime.dependency_builds.save(record)
        with patch.object(self.runtime.dependency_builds, "start"):
            observed = self.runtime.dependency_builds.status(self.request["build_id"])
        self.assertEqual(observed["state"], "qualifying")
        self.assertEqual(observed["image"], engine.image)
        self.assertEqual([value[0] for value in engine.commands].count("commit"), 1)

    def test_late_commit_completion_is_reconciled_after_reopen(self):
        record = self.assembly()
        engine = AssemblyEngine(self.profile.image, lose_response=True, late=True)
        self.runtime.docker = engine
        with self.assertRaises(EngineError):
            self.runtime.dependency_builds.assemble(record)
        record["state"] = "unknown"
        self.runtime.dependency_builds.save(record)
        self.runtime.close()
        self.runtime = Runtime(
            Path(self.temporary.name),
            self.profile.executor_profiles() + [self.profile.setup_profile()],
            docker=engine,
        )
        self.addCleanup(self.runtime.close)
        self.assertEqual(
            self.runtime.dependency_builds.status(self.request["build_id"])["state"],
            "unknown",
        )
        engine.images[engine.image] = engine.pending
        with patch.object(self.runtime.dependency_builds, "start"):
            self.assertEqual(
                self.runtime.dependency_builds.status(self.request["build_id"])[
                    "state"
                ],
                "qualifying",
            )
        self.assertEqual([value[0] for value in engine.commands].count("commit"), 1)

    def test_multiple_matching_commits_remain_unknown(self):
        record = self.assembly()
        engine = AssemblyEngine(self.profile.image, lose_response=True)
        self.runtime.docker = engine
        with self.assertRaises(EngineError):
            self.runtime.dependency_builds.assemble(record)
        duplicate = dict(engine.images[engine.image], Id="sha256:" + "e" * 64)
        engine.images[duplicate["Id"]] = duplicate
        record["state"] = "unknown"
        self.runtime.dependency_builds.save(record)
        with patch.object(self.runtime.dependency_builds, "start") as start:
            self.assertEqual(
                self.runtime.dependency_builds.status(self.request["build_id"])[
                    "state"
                ],
                "unknown",
            )
        start.assert_not_called()

    def test_cleanup_does_not_remove_a_replacement_assembly_container(self):
        record = self.assembly()
        record["assembly_container_id"] = "original-container"
        engine = AssemblyEngine(self.profile.image)
        engine.current = {
            "Id": "replacement",
            "Image": self.profile.image,
            "Config": {
                "Labels": self.runtime.dependency_builds.assembly_labels(record)
            },
        }
        self.runtime.docker = engine
        with self.assertRaises(EngineError):
            self.runtime.dependency_builds.cleanup_assembly(record)
        self.assertIsNotNone(engine.current)

    def test_binding_change_during_qualification_preserves_unknown_fence(self):
        self.prepare()
        record = self.runtime.dependency_builds.read(self.request["build_id"])
        record["state"] = "qualifying"
        self.runtime.dependency_builds.save(record)
        with patch(
            "vibearr_executor.qualification.binding", return_value={"host": "changed"}
        ):
            self.runtime.dependency_builds.run(self.request["build_id"])
        self.assertEqual(
            self.runtime.dependency_builds.status(self.request["build_id"])["state"],
            "unknown",
        )
        self.assertTrue(self.runtime.dependency_builds.blocked(str(uuid4())))

    def test_failed_qualification_releases_fence_only_after_resource_cleanup(self):
        self.prepare()
        record = self.runtime.dependency_builds.read(self.request["build_id"])
        record["state"] = "qualifying"
        self.runtime.dependency_builds.save(record)
        path = (
            self.runtime.dependency_builds.path(self.request["build_id"])
            / "qualification-exit.json"
        )
        path.write_text('{"success": false}')
        with (
            patch(
                "vibearr_executor.qualification.binding", return_value={"host": "test"}
            ),
            patch.object(self.runtime.dependency_builds, "processes", return_value=[]),
            patch.object(
                self.runtime.dependency_builds, "cleanup_qualification"
            ) as cleanup,
        ):
            self.runtime.dependency_builds.run(self.request["build_id"])
        cleanup.assert_called_once()
        self.assertEqual(
            self.runtime.dependency_builds.status(self.request["build_id"])["state"],
            "failed",
        )
        self.assertFalse(self.runtime.dependency_builds.blocked(str(uuid4())))


class AssemblyEngine:
    def __init__(self, parent, *, lose_response=False, late=False):
        self.parent = parent
        self.lose_response = lose_response
        self.late = late
        self.current = None
        self.commands = []
        self.image = "sha256:" + "d" * 64
        self.images = {
            parent: {
                "Id": parent,
                "Config": {"Labels": {}},
                "RootFS": {"Layers": ["base-layer"]},
            }
        }

    def run(self, *arguments, **_options):
        self.commands.append(arguments)
        if arguments[:2] == ("image", "inspect"):
            return json.dumps([self.images[arguments[2]]]).encode()
        if arguments[:2] == ("image", "ls"):
            return "\n".join(
                image for image in self.images if image != self.parent
            ).encode()
        if arguments[0] == "create":
            labels = dict(
                arguments[index + 1].split("=", 1)
                for index, value in enumerate(arguments)
                if value == "--label"
            )
            self.current = {
                "Id": "original-container",
                "Image": self.parent,
                "State": {"Status": "created"},
                "Config": {"Labels": labels},
            }
        if arguments[0] == "commit":
            self.pending = {
                "Id": self.image,
                "Parent": self.parent,
                "Config": {"Labels": self.current["Config"]["Labels"]},
                "RootFS": {"Layers": ["base-layer", "candidate-layer"]},
            }
            if not self.late:
                self.images[self.image] = self.pending
            if self.lose_response:
                raise EngineError("Lost response; daemon commit may still finish")
            return self.image.encode()
        return b""

    def inspect(self, _name):
        return self.current

    def remove(self, _kind, _name):
        self.current = None
