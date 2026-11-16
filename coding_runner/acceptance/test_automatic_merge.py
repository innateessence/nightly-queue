from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import tempfile
import unittest
from uuid import uuid4

from coding_runner.test_automatic_merge import MergeHost
from vibearr_runner.native_contract import intent_document
from vibearr_runner.native_lifecycle import NativeLifecycle
from vibearr_runner.native_lifecycle import NativeFailure
from vibearr_runner.native_revision import NativeRevision
from vibearr_runner.native_validation import CandidateSource
from vibearr_runner.workspaces import Workspace, git


class PublicationRevisionAcceptanceTests(unittest.TestCase):
    def commit(self, checkout, title):
        git(["add", "--all"], checkout)
        git(
            [
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@localhost",
                "-c",
                "commit.gpgsign=false",
                "commit",
                "--message",
                title,
            ],
            checkout,
        )
        return git(["rev-parse", "HEAD"], checkout)

    def test_base_update_preserves_delivered_history_and_requires_fresh_validation(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkout = root / "checkout"
            git(["init", "--initial-branch=main", str(checkout)])
            (checkout / "requirements.txt").write_text("Django==5.2.8\n")
            base = self.commit(checkout, "Initial base")
            git(["checkout", "-b", "task/1"], checkout)
            (checkout / "task.py").write_text("value = 1\n")
            delivered = self.commit(checkout, "Task change")
            git(["checkout", "main"], checkout)
            (checkout / "base.py").write_text("base = 2\n")
            updated_base = self.commit(checkout, "Independent base change")
            git(["checkout", "task/1"], checkout)
            host = MergeHost()
            original = CandidateSource.from_git(checkout, delivered)
            identity = {
                **host.native["delivery"]["workspace"],
                "base_revision": base,
                "commit_sha": delivered,
                "tree_sha": original.tree_sha,
            }
            previous_id, agent_id = str(uuid4()), str(uuid4())
            host.native.update(
                workspace_id=previous_id,
                candidate={
                    "workspace": identity,
                    "agent_intent_id": agent_id,
                    "export_digest": "f" * 64,
                    "source_digest": original.digest,
                },
            )
            host.native["delivery"]["workspace"] = identity
            host.native["delivery"]["pull_request"]["head_sha"] = delivered
            local = Workspace(
                host.run_id,
                "https://github.com/owner/repo.git",
                "main",
                "main",
                base,
                "task/1",
                root,
            )
            host.workspace = lambda: local
            host.journal = SimpleNamespace(save=lambda state: None)
            host.state = {"native": host.native}
            host.artifact = lambda *args: str(uuid4())
            intent = intent_document(
                "revision",
                **{**host.context_binding(), "attempt": 1},
                data={
                    "publication_id": host.native["delivery"]["publication_id"],
                    "workspace_id": previous_id,
                    "workspace": identity,
                    "pull_request": host.native["delivery"]["pull_request"],
                    "base_revision": updated_base,
                    "reason": "base_update",
                },
            )
            host.native["publication_revision"] = {
                "intent_id": str(uuid4()),
                "intent": intent,
                "detail": "Base moved",
                "fetched": True,
            }
            host.native["validation"] = {"evidence": {"tree_sha": original.tree_sha}}
            host.native["ci_deadline"] = "2000-01-01T00:00:00+00:00"
            host.native["deadline:validation_timeout_seconds"] = (
                "2000-01-01T00:00:00+00:00"
            )
            original_deadline = host.native["deadline"]
            self.assertEqual(NativeRevision(host).step(), "continue")
            self.assertNotIn("ci_deadline", host.native)
            self.assertNotIn("deadline:validation_timeout_seconds", host.native)
            self.assertEqual(host.native["deadline"], original_deadline)
            current = host.native["candidate"]
            self.assertEqual(current["parents"], [delivered, updated_base])
            self.assertEqual(current["workspace"]["base_revision"], updated_base)
            self.assertEqual(current["agent_intent_id"], agent_id)
            self.assertEqual(host.native["attempt"], 1)
            self.assertEqual(host.native["phase"], "validation")
            self.assertNotIn("validation", host.native)
            self.assertEqual(
                host.native["publication_history"][0]["workspace"]["commit_sha"],
                delivered,
            )
            self.assertEqual(host.native["delivery"]["existing_pull_number"], 9)
            self.assertEqual(host.native["delivery"]["previous_head"], delivered)
            git(
                [
                    "merge-base",
                    "--is-ancestor",
                    delivered,
                    current["workspace"]["commit_sha"],
                ],
                checkout,
            )
            git(
                [
                    "merge-base",
                    "--is-ancestor",
                    updated_base,
                    current["workspace"]["commit_sha"],
                ],
                checkout,
            )
            self.assertTrue((checkout / "task.py").is_file())
            self.assertTrue((checkout / "base.py").is_file())
            deadline = host.native["deadline"]
            host.native["implementation_timeout_seconds"] = "2026-11-16T22:00:00+00:00"
            host.native["validation_timeout_seconds"] = "2026-11-16T22:10:00+00:00"
            host.native["validation"] = {
                "failed": {"check_id": "django", "exit_code": 1}
            }
            failed = subprocess.run(
                [sys.executable, "-c", "raise SystemExit(1)"], check=False
            )
            self.assertEqual(failed.returncode, 1)
            host.native["dependency"] = {"old": "validation setup"}
            host.executor = SimpleNamespace(
                logs=lambda *args: b"actual validation diagnostic"
            )
            host.native["delivery_recovery_generation"] = 1
            with self.assertRaises(NativeFailure):
                NativeLifecycle.repair(host, {"check_id": "django"})
            self.assertEqual(host.native["attempt"], 1)
            host.native.pop("delivery_recovery_generation")
            self.assertEqual(
                NativeLifecycle.repair(
                    host,
                    {"check_id": "django", "operation": {"operation_id": str(uuid4())}},
                ),
                "continue",
            )
            self.assertEqual(host.native["attempt"], 2)
            self.assertEqual(host.native["phase"], "agent")
            self.assertNotIn("validation", host.native)
            self.assertNotIn("dependency", host.native)
            self.assertEqual(
                host.native["candidate"]["workspace"]["commit_sha"],
                current["workspace"]["commit_sha"],
            )
            self.assertEqual(host.native["deadline"], deadline)
            self.assertEqual(
                host.native["implementation_timeout_seconds"],
                "2026-11-16T22:00:00+00:00",
            )
            self.assertEqual(
                host.native["validation_timeout_seconds"], "2026-11-16T22:10:00+00:00"
            )
            (checkout / "task.py").write_text("value = 2\n")
            repaired = self.commit(
                checkout, "Repair task after failed local validation"
            )
            host.native["candidate"]["workspace"]["commit_sha"] = repaired
            NativeRevision(host).activate_candidate()
            self.assertNotIn("workspace", host.native["delivery"])
            self.assertEqual(host.native["delivery"]["previous_head"], delivered)
            self.assertEqual(host.native["delivery"]["existing_pull_number"], 9)
            self.assertEqual(len(host.native["publication_history"]), 1)


if __name__ == "__main__":
    unittest.main()
