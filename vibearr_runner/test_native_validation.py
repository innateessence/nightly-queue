from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import subprocess
import shutil
import importlib.util
import base64
from unittest.mock import patch
import tempfile
import unittest
from uuid import uuid4

from vibearr_runner.native_validation import CandidateSource, ValidationRunner
from vibearr_runner.native_validation import DependencyBuilder, validation_diagnostics
from vibearr_runner.policy import ExecutionPolicy

SDK_AVAILABLE = importlib.util.find_spec("vibearr_executor") is not None
if SDK_AVAILABLE:
    from vibearr_executor.artifacts import pack, unpack, result_manifest
    from vibearr_executor.contracts import Conflict, Rejected, digest
    from vibearr_executor.validation_profile import CHECK_IDS, ValidationProfile


@unittest.skipUnless(
    SDK_AVAILABLE,
    "Install the native-runtime extra for executor-backed validation tests",
)
@unittest.skipUnless(
    shutil.which("git"),
    "Exact-tree Git acceptance runs in the independent runner image",
)
class CandidateSourceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.git("init", "-q")

    def git(self, *arguments):
        return (
            subprocess.check_output(
                [
                    "git",
                    "-c",
                    "core.hooksPath=/dev/null",
                    "-c",
                    "user.name=Validation",
                    "-c",
                    "user.email=validation@example.invalid",
                    "-c",
                    "commit.gpgsign=false",
                    "-C",
                    str(self.root),
                    *arguments,
                ],
                stderr=subprocess.DEVNULL,
            )
            .decode()
            .strip()
        )

    def commit(self):
        self.git("add", "-f", ".")
        self.git("commit", "-qm", "Candidate")
        return self.git("rev-parse", "HEAD")

    def test_source_uses_commit_instead_of_dirty_or_ignored_files(self):
        (self.root / "tracked.py").write_text("original = True\n")
        commit = self.commit()
        (self.root / "tracked.py").write_text("dirty = True\n")
        (self.root / "untracked.py").write_text("leftover = True\n")
        result = CandidateSource.from_git(self.root, commit)
        self.assertEqual(
            unpack(result.archive), {"tracked.py": (0o644, b"original = True\n")}
        )
        self.assertEqual(result.tree_sha, self.git("rev-parse", commit + "^{tree}"))

    def test_ignored_tracked_python_is_still_in_source(self):
        (self.root / ".gitignore").write_text("*\n")
        (self.root / "broken.py").write_text("this is deliberately malformed !!!\n")
        result = CandidateSource.from_git(self.root, self.commit())
        self.assertIn("broken.py", unpack(result.archive))

    def test_source_rejects_symlinks(self):
        (self.root / "linked").symlink_to("/etc/passwd")
        with self.assertRaises(Rejected):
            CandidateSource.from_git(self.root, self.commit())

    def test_source_requires_exact_commit(self):
        with self.assertRaises(Rejected):
            CandidateSource.from_git(self.root, "HEAD")


class ClientFixture:
    def __init__(self, profile):
        self.profile = profile
        self.manifests = {}
        self.states = {}
        self.started = []
        self.source = b""

    def capabilities(self):
        from vibearr_executor import (
            SLOT_PROTOCOL_VERSION as PROTOCOL_VERSION,
            SLOT_QUALIFICATION_VERSION as QUALIFICATION_VERSION,
        )

        return {
            "schema_version": 1,
            "runtime": "compact-worker-v1",
            "protocol_version": PROTOCOL_VERSION,
            "qualification_version": QUALIFICATION_VERSION,
            "qualified": True,
            "accepting_work": True,
            "profiles": [
                {"profile_id": value.profile_id, "profile_digest": value.digest}
                for value in self.profile.executor_profiles()
            ],
        }

    def upload(self, _kind, content):
        if _kind == "source":
            self.source = content
        return {"sha256": digest(content)}

    def operation(self, _operation, action):
        assert action == "result"
        return {
            "manifest": result_manifest(self.source, self.source),
            "content": base64.b64encode(self.source).decode(),
        }

    def prepare(self, manifest):
        operation = manifest["operation_id"]
        if operation in self.manifests and self.manifests[operation] != manifest:
            raise Conflict("Changed manifest")
        self.manifests[operation] = manifest
        self.states.setdefault(
            operation, {"operation_id": operation, "state": "not-started"}
        )

    def status(self, operation):
        return self.states[operation]

    def start(self, operation):
        self.started.append(operation)
        self.states[operation] = {
            "operation_id": operation,
            "state": "exited",
            "descendants_terminated": True,
            "result": {
                "run_id": self.manifests[operation]["run_id"],
                "operation_id": operation,
                "exit_code": 0,
                "started_at": 1,
                "ended_at": 2,
                "timed_out": False,
                "canceled": False,
                "log_truncated": False,
                "log_digest": "a" * 64,
                "artifact": result_manifest(self.source, self.source),
            },
        }

    def stop(self, operation):
        self.states[operation] = {
            "operation_id": operation,
            "state": "exited",
            "descendants_terminated": True,
            "result": {"exit_code": None},
        }


@unittest.skipUnless(
    SDK_AVAILABLE,
    "Install the native-runtime extra for executor-backed validation tests",
)
class NativeValidationTests(unittest.TestCase):
    def setUp(self):
        requirements = b"Django==4.1.3\n"
        self.source = CandidateSource(
            "a" * 40, "b" * 40, pack({"requirements.txt": (0o644, requirements)})
        )
        self.profile = ValidationProfile(
            "sha256:" + "a" * 64, "sha256:" + "b" * 64, digest(requirements)
        )
        self.client = ClientFixture(self.profile)
        self.runner = ValidationRunner(self.client)
        self.policy = replace(
            ExecutionPolicy(), validation_profile=self.profile.profile_id
        )

    def prepare(self, **overrides):
        data = {
            "run_id": str(uuid4()),
            "attempt_id": str(uuid4()),
            "source": self.source,
            "profile": self.profile,
            "policy": self.policy,
            "deadline": (
                datetime.now(timezone.utc) + timedelta(minutes=10)
            ).isoformat(),
        }
        data.update(overrides)
        return self.runner.prepare(**data)

    def test_prepare_and_recovery_preserve_all_operation_identities(self):
        session = self.prepare()
        for _ in CHECK_IDS:
            self.runner.start(session)
        observed = ValidationRunner(self.client).status(session)
        self.assertEqual(observed["state"], "passed")
        self.assertEqual(
            [value["id"] for value in observed["validation"]["checks"]], list(CHECK_IDS)
        )
        self.assertEqual(len(set(self.client.started)), len(CHECK_IDS))
        self.runner.start(session)
        self.assertEqual(len(self.client.started), len(CHECK_IDS))

    def test_recovered_tree_label_must_match_prepared_context(self):
        session = self.prepare()
        for _ in CHECK_IDS:
            self.runner.start(session)
        changed = deepcopy(session)
        changed["tree_sha"] = "c" * 40
        recovered = ValidationRunner(self.client)
        with self.assertRaises(Rejected):
            recovered.status(changed)
        self.assertEqual(recovered.status(session)["state"], "passed")

    def test_consistently_changed_context_cannot_start_validation(self):
        session = self.prepare()
        for entry in session["operations"]:
            entry["manifest"]["context_digest"] = "d" * 64
        with self.assertRaises(Rejected):
            self.runner.start(session)
        self.assertEqual(self.client.started, [])

    def test_missing_check_cannot_be_attested_as_passing(self):
        session = self.prepare()
        session["operations"] = []
        with self.assertRaises(Rejected):
            self.runner.status(session)

    def test_changed_command_manifest_is_rejected(self):
        session = self.prepare()
        session["operations"][0]["manifest"]["profile_id"] = "other"
        with self.assertRaises(Rejected):
            self.runner.start(session)

    def test_unknown_outcome_does_not_start_a_replacement(self):
        session = self.prepare()
        operation = session["operations"][0]["manifest"]["operation_id"]
        self.client.states[operation]["state"] = "unknown"
        self.assertEqual(self.runner.start(session)["state"], "unknown")
        self.assertEqual(self.client.started, [])

    def test_failed_check_stops_later_check_dispatch(self):
        session = self.prepare()
        self.runner.start(session)
        operation = self.client.started[0]
        self.client.states[operation]["result"]["exit_code"] = 1
        self.assertEqual(self.runner.start(session)["state"], "failed")
        self.assertEqual(self.client.started, [operation])

    def test_truncated_logs_cannot_qualify_success(self):
        session = self.prepare()
        self.runner.start(session)
        self.client.states[self.client.started[0]]["result"]["log_truncated"] = True
        self.assertEqual(self.runner.status(session)["state"], "failed")

    def test_missing_export_cannot_qualify_success(self):
        session = self.prepare()
        self.runner.start(session)
        self.client.states[self.client.started[0]]["result"].pop("artifact")
        self.assertEqual(self.runner.status(session)["state"], "failed")

    def test_export_error_cannot_qualify_success(self):
        session = self.prepare()
        self.runner.start(session)
        self.client.states[self.client.started[0]]["result"][
            "export_error"
        ] = "Unsafe workspace"
        self.assertEqual(self.runner.status(session)["state"], "failed")

    def test_export_bytes_are_verified_before_success(self):
        session = self.prepare()
        operation = session["operations"][0]["manifest"]["operation_id"]
        self.client.start(operation)
        response = self.client.operation(operation, "result")
        response["content"] = base64.b64encode(
            pack({"other": (0o644, b"different")})
        ).decode()
        with (
            patch.object(self.client, "operation", return_value=response),
            self.assertRaises(Rejected),
        ):
            self.runner.status(session)

    def test_mismatched_result_identity_cannot_qualify_success(self):
        session = self.prepare()
        self.runner.start(session)
        self.client.states[self.client.started[0]]["result"]["run_id"] = str(uuid4())
        with self.assertRaises(Rejected):
            self.runner.status(session)

    def test_invalid_result_timestamps_raise_a_bounded_diagnostic(self):
        session = self.prepare()
        self.runner.start(session)
        self.client.states[self.client.started[0]]["result"]["ended_at"] = float("inf")
        with self.assertRaises(Rejected):
            self.runner.status(session)

    def test_changed_dependencies_require_new_image(self):
        profile = replace(self.profile, dependency_digest="f" * 64)
        with self.assertRaises(Rejected):
            self.prepare(profile=profile)

    def test_resource_manifest_uses_frozen_policy_limits(self):
        policy = replace(
            self.policy, cpu_limit=1, memory_mb=1024, workspace_mb=256, pids_limit=32
        )
        session = self.prepare(policy=policy)
        self.assertEqual(
            session["operations"][0]["manifest"]["limits"],
            {"cpu": 1, "memory_mb": 1024, "pids": 32, "workspace_mb": 256},
        )

    def test_old_recipe_remains_cancelable_without_new_dispatch(self):
        session = self.prepare()
        session["profile"]["recipe_digest"] = "f" * 64
        with self.assertRaises(Rejected):
            self.runner.start(session)
        self.assertTrue(self.runner.stop(session)["terminated"])
        self.assertEqual(self.client.started, [])

    def test_semantic_profile_diagnostics_checks_every_installed_command(self):
        result = validation_diagnostics(self.policy, self.client, self.profile)
        self.assertTrue(result["ready"])
        self.assertEqual(
            result["profile"]["profile_id"], self.policy.validation_profile
        )

    def test_dependency_build_cannot_add_package_hosts_to_policy(self):
        builder = DependencyBuilder(self.client)
        policy = replace(self.policy, network_hosts=("api.openai.com",))
        with self.assertRaises(Rejected):
            builder.prepare(
                run_id=str(uuid4()),
                build_id=str(uuid4()),
                source=self.source,
                profile=self.profile,
                policy=policy,
                deadline=(
                    datetime.now(timezone.utc) + timedelta(minutes=5)
                ).isoformat(),
            )
