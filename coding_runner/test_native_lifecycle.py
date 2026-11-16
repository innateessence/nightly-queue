import base64
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

from vibearr_executor import PROTOCOL_VERSION, QUALIFICATION_VERSION
from vibearr_executor.artifacts import pack, unpack, result_manifest
from vibearr_executor.contracts import canonical, digest
from vibearr_executor.validation_profile import ValidationProfile
from vibearr_runner.context import manual_context
from vibearr_runner.journal import Journal
from vibearr_runner.native_codex import codex_profile
from vibearr_runner.native_lifecycle import NativeLifecycle
from vibearr_runner.policy import ExecutionPolicy
from vibearr_runner.recovery import ReconciliationRequired
from vibearr_runner.workspaces import Workspace, git


class Executor:
    def __init__(self, profile):
        self.instance = str(uuid4())
        self.agent = codex_profile("sha256:" + "a" * 64, "token")
        self.profiles = [
            self.agent,
            *profile.executor_profiles(),
            profile.setup_profile(),
        ]
        self.artifacts, self.manifests, self.states, self.outputs, self.logfiles = (
            {},
            {},
            {},
            {},
            {},
        )
        self.started = []
        self.no_change = False
        self.validation_failures = 0
        self.unknown = set()

    def capabilities(self):
        return {
            "schema_version": 1,
            "runtime": "docker-oci-v1",
            "protocol_version": PROTOCOL_VERSION,
            "qualification_version": QUALIFICATION_VERSION,
            "qualified": True,
            "accepting_work": True,
            "binding_digest": "e" * 64,
            "instance_id": self.instance,
            "profiles": [
                {**profile.data(), "profile_digest": profile.digest}
                for profile in self.profiles
            ],
        }

    def upload(self, kind, content):
        self.artifacts[digest(content)] = content
        return {"sha256": digest(content)}

    def prepare(self, manifest):
        identity = manifest["operation_id"]
        if identity in self.manifests:
            assert self.manifests[identity] == manifest
        else:
            self.manifests[identity] = deepcopy(manifest)
            self.states[identity] = {"operation_id": identity, "state": "not-started"}
        return self.status(identity)

    def supply_secret(self, *args):
        return {}

    def start(self, identity):
        if self.states[identity]["state"] != "not-started":
            return self.status(identity)
        self.started.append(identity)
        manifest = self.manifests[identity]
        source = self.artifacts[manifest["source_digest"]]
        output = source
        agent = manifest["profile_id"] == self.agent.profile_id
        if agent and not self.no_change:
            files = unpack(source)
            files["answer.py"] = (0o644, f"answer = {len(self.started) + 1}\n".encode())
            output = pack(files)
        logs = b""
        if agent:
            logs = (
                json.dumps({"type": "thread.started", "thread_id": str(uuid4())})
                + "\n"
                + json.dumps(
                    {
                        "type": "turn.completed",
                        "usage": {"input_tokens": 10, "output_tokens": 5},
                    }
                )
                + "\n"
            ).encode()
        failed = not agent and self.validation_failures > 0
        if failed:
            self.validation_failures -= 1
        self.logfiles[identity] = logs
        self.outputs[identity] = output
        now = datetime.now(timezone.utc).timestamp()
        self.states[identity] = {
            "operation_id": identity,
            "state": "exited",
            "descendants_terminated": True,
            "container_id": digest(identity.encode()),
            "result": {
                "run_id": manifest["run_id"],
                "operation_id": identity,
                "terminated": True,
                "started_at": now,
                "ended_at": now,
                "exit_code": 1 if failed else 0,
                "timed_out": False,
                "canceled": False,
                "log_truncated": False,
                "log_digest": digest(logs),
                "artifact": result_manifest(source, output),
            },
        }
        return self.status(identity)

    def status(self, identity):
        if identity in self.unknown:
            return {"operation_id": identity, "state": "unknown"}
        return deepcopy(self.states[identity])

    def stop(self, identity):
        if self.states[identity]["state"] == "not-started":
            manifest = self.manifests[identity]
            self.states[identity] = {
                "operation_id": identity,
                "state": "exited",
                "descendants_terminated": True,
                "result": {
                    "run_id": manifest["run_id"],
                    "operation_id": identity,
                    "terminated": True,
                    "exit_code": None,
                    "canceled": True,
                },
            }
        return self.status(identity)

    def logs(self, identity, offset=0, limit=65536):
        return self.logfiles.get(identity, b"")[offset : offset + limit]

    def export(self, identity, source):
        output = self.outputs[identity]
        return result_manifest(source, output), output

    def operation(self, identity, action):
        assert action == "result"
        source = self.artifacts[self.manifests[identity]["source_digest"]]
        output = self.outputs[identity]
        return {
            "manifest": result_manifest(source, output),
            "content": base64.b64encode(output).decode(),
        }


class Queue:
    def __init__(self, source):
        self.source = source
        self.artifacts = []
        self.finishes = []
        self.fail_source = False

    def request(self, method, path, payload=None):
        if path.endswith("/source/refresh/"):
            return {
                "eligible": not self.fail_source,
                "reason": "Source closed" if self.fail_source else "",
                "source_snapshot": self.source,
                "repository": {
                    "repository_id": 17,
                    "repository_url": "https://github.com/owner/repo.git",
                    "default_branch": "main",
                    "owner_id": 1,
                },
            }
        if path.endswith("/secrets/"):
            return {
                "credential": {
                    "id": 1,
                    "provider": "codex",
                    "kind": "token",
                    "version": 2,
                },
                "material": {"secret": "fixture-codex-token-0123456789"},
            }
        if path.endswith("/contexts/") or path.endswith("/plans/"):
            name = "context" if path.endswith("/contexts/") else "plan"
            return {
                name: {
                    "id": payload["operation_id"],
                    "digest": digest(canonical(payload["document"])),
                    "document": payload["document"],
                }
            }
        if path.endswith("/artifacts/"):
            self.artifacts.append(deepcopy(payload))
        if path.endswith("/finish/"):
            self.finishes.append(deepcopy(payload))
        return {}


class NativeLifecycleTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.journal = Journal(self.root / "journal")
        self.journal.__enter__()
        self.addCleanup(self.journal.__exit__)
        self.run_id = str(uuid4())
        directory = self.root / "workspaces" / self.run_id
        checkout = directory / "checkout"
        checkout.mkdir(parents=True)
        git(["init", "-b", "main", str(checkout)], local=True)
        (checkout / "requirements.txt").write_bytes(b"")
        (checkout / "answer.py").write_text("answer = 1\n")
        git(["add", "."], checkout)
        git(
            [
                "-c",
                "user.name=fixture",
                "-c",
                "user.email=fixture@example.test",
                "commit",
                "-m",
                "Base",
            ],
            checkout,
        )
        revision = git(["rev-parse", "HEAD"], checkout)
        git(["update-ref", "refs/remotes/origin/main", revision], checkout)
        git(["checkout", "-b", "task/native"], checkout)
        self.workspace = Workspace(
            self.run_id,
            "https://github.com/owner/repo.git",
            "main",
            "main",
            revision,
            "task/native",
            directory,
        )
        (directory / "workspace.json").write_text(
            json.dumps({**asdict(self.workspace), "directory": str(directory)})
        )
        self.profile = ValidationProfile(
            "sha256:" + "b" * 64, "sha256:" + "c" * 64, hashlib.sha256(b"").hexdigest()
        )
        self.executor = Executor(self.profile)
        policy = replace(
            ExecutionPolicy(),
            runtime_profile=self.executor.agent.profile_id,
            validation_profile=self.profile.profile_id,
        )
        item = {
            "title": "Update the answer",
            "instructions": "Improve answer.py",
            "source": "manual",
            "repository_url": self.workspace.repository_url,
            "base_branch": "main",
            "branch_name": "task/native",
            "claim_token": str(uuid4()),
        }
        self.queue = Queue(manual_context(item))
        self.host = SimpleNamespace(
            client=self.queue,
            journal=self.journal,
            stop=threading.Event(),
            checkpoint=lambda: True,
            workspaces=SimpleNamespace(
                directory=directory.parent, prepare=lambda *args: self.workspace
            ),
        )
        self.state = self.journal.load()
        self.state.update(
            runner_id=1,
            run={
                "run": {
                    "id": self.run_id,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "configuration_revision": "f" * 64,
                    "configuration_snapshot": {
                        "execution_kind": "native",
                        "execution_policy": policy.to_data(),
                        "global": {
                            "task_timeout_seconds": 3600,
                            "implementation_timeout_seconds": 1200,
                            "validation_timeout_seconds": 1200,
                        },
                        "effective": {
                            "implementation_backend": "codex",
                            "implementation_model": "gpt-6.1-sol",
                        },
                    },
                },
                "item": item,
            },
        )
        github = patch(
            "vibearr_runner.native_github.NativeGitHub.repository",
            return_value=({"repository_url": self.workspace.repository_url}, {}),
        )
        github.start()
        self.addCleanup(github.stop)

    def engine(self):
        return NativeLifecycle(
            self.host, executor=self.executor, validation_profile=self.profile
        )

    def advance(self, phase):
        for _ in range(50):
            if (
                self.state.get("native", {}).get("phase") == phase
                or self.queue.finishes
            ):
                return
            self.engine().step(self.state)
        self.fail("Native fixture did not reach " + phase)

    def test_restart_after_agent_completion_validates_exact_commit_without_relaunch(
        self,
    ):
        self.advance("import_candidate")
        starts = list(self.executor.started)
        self.state = self.journal.load()
        self.advance("delivery")
        self.assertEqual(self.executor.started[:1], starts)
        self.assertEqual(len(self.executor.started), 10)
        self.assertEqual(
            git(["rev-parse", "HEAD^{tree}"], self.workspace.checkout),
            self.state["native"]["validation"]["evidence"]["tree_sha"],
        )

    def test_failed_validation_repairs_once_after_every_prior_operation_terminated(
        self,
    ):
        self.executor.validation_failures = 1
        self.advance("delivery")
        self.assertEqual(self.state["native"]["attempt"], 1)
        agents = [
            entry for entry in self.queue.artifacts if entry["kind"] == "native_agent"
        ]
        self.assertEqual(len(agents), 2)
        self.assertTrue(
            all(value["state"] == "exited" for value in self.executor.states.values())
        )

    def test_unknown_agent_never_selects_a_replacement_session(self):
        self.advance("agent")
        self.engine().step(self.state)
        operation = self.executor.started[0]
        self.executor.unknown.add(operation)
        with self.assertRaises(ReconciliationRequired):
            self.engine().step(self.state)
        self.assertEqual(self.executor.started, [operation])

    def test_no_change_finishes_with_explanation_and_no_commit(self):
        self.executor.no_change = True
        self.advance("delivery")
        self.assertEqual(self.queue.finishes[0]["outcome"], "no_change")
        self.assertEqual(
            git(["rev-list", "--count", "HEAD"], self.workspace.checkout), "1"
        )

    def test_source_closure_stops_owned_native_work_before_finish(self):
        self.advance("import_candidate")
        self.queue.fail_source = True
        self.state["native"]["source_checked_at"] = 0
        self.engine().step(self.state)
        self.assertEqual(self.queue.finishes[0]["status"], "failed")
        termination = [
            value
            for value in self.queue.artifacts
            if value["kind"] == "native_termination"
        ]
        self.assertEqual(len(termination), 1)

    def test_repair_allowance_is_bounded_to_two_new_sessions(self):
        self.executor.validation_failures = 3
        self.advance("delivery")
        self.assertEqual(self.state["native"]["attempt"], 2)
        self.assertEqual(
            len(
                [
                    value
                    for value in self.queue.artifacts
                    if value["kind"] == "native_agent"
                ]
            ),
            3,
        )
        self.assertEqual(self.queue.finishes[0]["outcome"], "failed")

    def test_cancel_with_unknown_executor_retains_claim_without_finish(self):
        self.advance("import_candidate")
        operation = self.executor.started[0]
        self.executor.unknown.add(operation)
        self.state["run"]["run"]["cancellation_requested_at"] = datetime.now(
            timezone.utc
        ).isoformat()
        with self.assertRaises(ReconciliationRequired):
            self.engine().step(self.state)
        self.assertEqual(self.queue.finishes, [])
