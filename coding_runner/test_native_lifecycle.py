import base64
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import shutil
import subprocess
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.error import URLError
from uuid import uuid4

from vibearr_runner.context import manual_context
from vibearr_runner.client import ConflictError, SupersededSourceError
from vibearr_runner.journal import Journal
from vibearr_runner.native_codex import codex_profile
from vibearr_runner.native_lifecycle import NativeLifecycle
from vibearr_runner.native_github import NativeGitHub
from vibearr_runner.policy import ExecutionPolicy
from vibearr_runner.recovery import ReconciliationRequired
from vibearr_runner.workspaces import Workspace, git
from vibearr_runner.workspaces import WorkspaceManager

SDK_AVAILABLE = importlib.util.find_spec("vibearr_executor") is not None
REPOSITORY_READ = NativeGitHub.repository
if SDK_AVAILABLE:
    from vibearr_executor import PROTOCOL_VERSION, QUALIFICATION_VERSION
    from vibearr_executor.artifacts import pack, unpack, result_manifest
    from vibearr_executor.contracts import canonical, digest
    from vibearr_executor.validation_profile import ValidationProfile


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
        self.default_branch = "main"

    def request(self, method, path, payload=None):
        if path.endswith("/source/refresh/"):
            return {
                "eligible": not self.fail_source,
                "reason": "Source closed" if self.fail_source else "",
                "source_snapshot": self.source,
                "repository": {
                    "repository_id": 17,
                    "repository_url": "https://github.com/owner/repo.git",
                    "default_branch": self.default_branch,
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
            return {
                "evidence": {
                    "operation_id": payload["operation_id"],
                    "kind": payload["kind"],
                    "data": payload["data"],
                }
            }
        if path.endswith("/finish/"):
            self.finishes.append(deepcopy(payload))
        return {}


@unittest.skipUnless(
    SDK_AVAILABLE and shutil.which("git"),
    "Native lifecycle tests require the runtime extra and Git",
)
class NativeLifecycleTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.journal = Journal(self.root / "journal")
        self.journal.__enter__()
        self.addCleanup(self.journal.__exit__)
        self.run_id = getattr(self, "supervision_run_id", str(uuid4()))
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
            return_value=(
                {
                    "repository_url": self.workspace.repository_url,
                    "default_branch": "main",
                },
                {},
            ),
        )
        github.start()
        self.addCleanup(github.stop)

    def engine(self):
        return NativeLifecycle(
            self.host, executor=self.executor, validation_profile=self.profile
        )

    def superseded_source(self):
        original = self.queue.request

        def request(method, path, payload=None):
            if path.endswith("/source/refresh/"):
                raise SupersededSourceError("Newer observation")
            return original(method, path, payload)

        return patch.object(self.queue, "request", side_effect=request)

    def test_superseded_source_pauses_and_resumes_same_agent_after_restart(self):
        self.advance("agent")
        self.assertEqual(self.engine().step(self.state), "running")
        operation = self.executor.started[0]
        self.executor.states[operation] = {
            "operation_id": operation,
            "state": "running",
            "container_id": "same-container",
        }
        self.executor.pause = Mock(return_value={"paused": True})
        self.executor.resume = Mock(return_value={"paused": False})
        self.state["native"]["source_checked_at"] = 0
        deadline = self.state["native"]["deadline"]
        agent_deadline = self.state["native"]["agent"]["session"]["manifest"][
            "deadline"
        ]
        with self.superseded_source():
            self.assertEqual(self.engine().step(self.state), "wait")
            self.state = self.journal.load()
            self.assertEqual(self.engine().step(self.state), "wait")
        self.assertTrue(self.state["native"]["paused"])
        reads = self.state["native"]["transport_reads"]
        self.assertEqual(next(iter(reads.values()))["attempts"], 1)
        next(iter(reads.values()))["retry_at"] = 0
        self.journal.save(self.state)
        self.state = self.journal.load()
        self.assertEqual(self.engine().step(self.state), "running")
        self.executor.pause.assert_called_with(operation)
        self.executor.resume.assert_called_once_with(operation)
        self.assertEqual(self.executor.started, [operation])
        self.assertEqual(
            self.executor.states[operation]["container_id"], "same-container"
        )
        self.assertEqual(self.state["native"]["deadline"], deadline)
        self.assertEqual(
            self.state["native"]["agent"]["session"]["manifest"]["deadline"],
            agent_deadline,
        )
        self.assertFalse(self.state["native"]["paused"])
        self.assertEqual(self.state["native"]["transport_reads"], {})
        self.assertEqual(self.queue.finishes, [])

    def test_superseded_source_exhausts_without_relaunching_completed_work(self):
        self.advance("delivery")
        starts = list(self.executor.started)
        self.state["native"]["source_checked_at"] = 0
        with self.superseded_source():
            for _ in range(2):
                self.assertEqual(self.engine().step(self.state), "wait")
                self.state = self.journal.load()
                for record in self.state["native"]["transport_reads"].values():
                    record["retry_at"] = 0
                self.journal.save(self.state)
            self.assertEqual(self.engine().step(self.state), "reporting_pending")
        self.assertEqual(self.executor.started, starts)
        self.assertEqual(len(self.queue.finishes), 1)
        self.assertEqual(self.queue.finishes[0]["outcome"], "failed")
        self.assertIn("three bounded attempts", self.queue.finishes[0]["error_summary"])

    def test_lost_source_pause_response_preserves_resume_after_restart(self):
        from vibearr_executor.client import ExecutorUnavailable

        self.advance("agent")
        self.engine().step(self.state)
        operation = self.executor.started[0]
        self.executor.states[operation] = {
            "operation_id": operation,
            "state": "running",
            "container_id": "same-container",
        }
        self.executor.pause = Mock(side_effect=ExecutorUnavailable("Lost pause reply"))
        self.executor.resume = Mock(return_value={"paused": False})
        self.state["native"]["source_checked_at"] = 0
        with self.superseded_source():
            with self.assertRaises(ReconciliationRequired):
                self.engine().step(self.state)
        self.state = self.journal.load()
        self.assertTrue(self.state["native"]["paused"])
        for record in self.state["native"]["transport_reads"].values():
            record["retry_at"] = 0
        self.assertEqual(self.engine().step(self.state), "running")
        self.executor.resume.assert_called_once_with(operation)
        self.assertEqual(self.executor.started, [operation])

    def test_cancel_during_source_recollection_stops_without_another_read(self):
        self.advance("delivery")
        starts = list(self.executor.started)
        self.state["native"]["source_checked_at"] = 0
        with self.superseded_source() as requests:
            self.assertEqual(self.engine().step(self.state), "wait")
            self.state["run"]["run"]["cancellation_requested_at"] = datetime.now(
                timezone.utc
            ).isoformat()
            self.assertEqual(self.engine().step(self.state), "reporting_pending")
        source_calls = [
            entry
            for entry in requests.call_args_list
            if entry.args[1].endswith("/source/refresh/")
        ]
        self.assertEqual(len(source_calls), 1)
        self.assertEqual(self.executor.started, starts)
        self.assertEqual(self.queue.finishes[0]["outcome"], "canceled")

    def test_generic_source_conflict_is_terminal_without_recollection(self):
        self.advance("agent")
        self.state["native"]["source_checked_at"] = 0
        original = self.queue.request

        def request(method, path, payload=None):
            if path.endswith("/source/refresh/"):
                raise ConflictError("Different source authority")
            return original(method, path, payload)

        with patch.object(self.queue, "request", side_effect=request):
            self.assertEqual(self.engine().step(self.state), "reporting_pending")
        self.assertEqual(self.executor.started, [])
        self.assertEqual(self.queue.finishes[0]["outcome"], "failed")

    def test_deadline_stops_source_recollection_without_a_new_agent(self):
        self.advance("agent")
        self.state["native"]["source_checked_at"] = 0
        with self.superseded_source():
            self.assertEqual(self.engine().step(self.state), "wait")
        self.state["native"]["deadline"] = "2000-01-01T00:00:00+00:00"
        self.assertEqual(self.engine().step(self.state), "reporting_pending")
        self.assertEqual(self.executor.started, [])
        self.assertIn("deadline", self.queue.finishes[0]["error_summary"])

    def transient_repository_read(self):
        with patch.object(NativeGitHub, "repository", REPOSITORY_READ):
            with patch(
                "urllib.request.OpenerDirector.open",
                side_effect=URLError("Temporary DNS failure"),
            ):
                return self.engine().step(self.state)

    def test_transient_repository_read_preserves_run_before_dispatch(self):
        self.assertEqual(self.transient_repository_read(), "wait")
        self.assertEqual(self.queue.finishes, [])
        self.assertEqual(self.executor.started, [])
        self.state = self.journal.load()
        self.assertEqual(self.transient_repository_read(), "wait")
        record = next(iter(self.state["native"]["transport_reads"].values()))
        self.assertEqual(record["attempts"], 1)

    def test_transient_repository_read_preserves_completed_agent_and_validation(self):
        self.advance("delivery")
        started = list(self.executor.started)
        self.assertEqual(self.transient_repository_read(), "wait")
        self.assertEqual(self.queue.finishes, [])
        self.assertEqual(self.executor.started, started)
        self.assertEqual(len(started), 10)

    def test_cancel_during_read_backoff_finishes_without_agent_dispatch(self):
        self.transient_repository_read()
        self.state["run"]["run"]["cancellation_requested_at"] = datetime.now(
            timezone.utc
        ).isoformat()
        self.assertEqual(self.engine().step(self.state), "reporting_pending")
        self.assertEqual(self.queue.finishes[0]["outcome"], "canceled")
        self.assertEqual(self.executor.started, [])

    def test_exhausted_repository_read_finishes_without_replacement_work(self):
        for _ in range(2):
            self.assertEqual(self.transient_repository_read(), "wait")
            for record in self.state["native"]["transport_reads"].values():
                record["retry_at"] = 0
        self.assertEqual(self.transient_repository_read(), "reporting_pending")
        self.assertEqual(len(self.queue.finishes), 1)
        self.assertIn("three bounded attempts", self.queue.finishes[0]["error_summary"])
        self.assertEqual(self.executor.started, [])

    def test_exhausted_failure_reconciliation_preserves_claim_fence(self):
        from vibearr_runner.native_reads import NativeReadExhausted

        self.transient_repository_read()
        self.state["native"]["failure"] = "Canceled by operator."
        self.state["native"]["outcome"] = "canceled"
        with patch(
            "vibearr_runner.native_delivery.NativeDelivery.reconcile_publications",
            side_effect=NativeReadExhausted("Read exhausted"),
        ):
            with self.assertRaises(ReconciliationRequired):
                self.engine().step(self.state)
        self.assertEqual(self.queue.finishes, [])

    def test_deadline_during_read_backoff_finishes_without_agent_dispatch(self):
        self.transient_repository_read()
        self.state["native"]["deadline"] = "2000-01-01T00:00:00+00:00"
        self.assertEqual(self.engine().step(self.state), "reporting_pending")
        self.assertEqual(self.queue.finishes[0]["outcome"], "failed")
        self.assertEqual(self.executor.started, [])

    def test_pause_during_read_backoff_prevents_another_attempt(self):
        self.transient_repository_read()
        self.host.checkpoint = lambda: False
        self.assertEqual(self.engine().step(self.state), "wait")
        self.assertTrue(self.state["native"]["paused"])
        record = next(iter(self.state["native"]["transport_reads"].values()))
        self.assertEqual(record["attempts"], 1)

    def test_partial_failed_clone_restarts_with_the_same_read_budget(self):
        from vibearr_runner import processes

        self.host.workspaces = WorkspaceManager(self.root / "fresh-workspaces")
        original = processes.run
        attempts = []

        def clone(arguments, **kwargs):
            if "clone" not in arguments:
                return original(arguments, **kwargs)
            attempts.append(arguments)
            checkout = Path(arguments[-1])
            if len(attempts) == 1:
                checkout.mkdir()
                (checkout / "partial").write_text("incomplete")
                return subprocess.CompletedProcess(
                    arguments, 128, b"", b"Could not resolve host: github.com"
                )
            self.assertFalse(checkout.exists())
            local = [
                "/usr/bin/git",
                "-c",
                "protocol.file.allow=always",
                "clone",
                "--no-checkout",
                "--no-local",
                "--single-branch",
                "--branch",
                "main",
                "--",
                str(self.workspace.checkout),
                str(checkout),
            ]
            return original(local, **kwargs)

        with patch("vibearr_runner.delivery.processes.run", side_effect=clone):
            self.assertEqual(self.engine().step(self.state), "wait")
            record = next(iter(self.state["native"]["transport_reads"].values()))
            self.assertEqual(record["attempts"], 1)
            record["retry_at"] = 0
            self.journal.save(self.state)
            self.state = self.journal.load()
            self.assertEqual(self.engine().step(self.state), "continue")
        self.assertEqual(len(attempts), 2)
        self.assertEqual(self.state["native"]["phase"], "agent")
        self.assertEqual(self.executor.started, [])
        self.assertEqual(self.queue.finishes, [])

    def test_completed_clone_restart_finishes_checkout_without_recloning(self):
        from vibearr_runner import processes
        from vibearr_runner import workspaces

        self.host.workspaces = WorkspaceManager(self.root / "fresh-workspaces")
        original, original_git = processes.run, workspaces.git
        attempts = []

        def clone(arguments, **kwargs):
            if "clone" not in arguments:
                return original(arguments, **kwargs)
            attempts.append(arguments)
            local = [
                "/usr/bin/git",
                "-c",
                "protocol.file.allow=always",
                "clone",
                "--no-checkout",
                "--no-local",
                "--single-branch",
                "--branch",
                "main",
                "--",
                str(self.workspace.checkout),
                arguments[-1],
            ]
            return original(local, **kwargs)

        def interrupt(arguments, *args, **kwargs):
            if arguments[:2] == ["symbolic-ref", "--short"]:
                raise KeyboardInterrupt("supervisor restart")
            return original_git(arguments, *args, **kwargs)

        with patch("vibearr_runner.delivery.processes.run", side_effect=clone):
            with patch("vibearr_runner.workspaces.git", side_effect=interrupt):
                with self.assertRaises(KeyboardInterrupt):
                    self.engine().step(self.state)
            self.state = self.journal.load()
            self.assertEqual(self.state["native"]["clone"]["state"], "completed")
            self.assertEqual(self.engine().step(self.state), "continue")
        self.assertEqual(len(attempts), 1)
        self.assertEqual(self.state["native"]["phase"], "agent")
        self.assertEqual(self.executor.started, [])

    def test_cancel_after_pause_inside_push_preflight_finishes_without_publication(
        self,
    ):
        from .test_native_delivery import GitHub
        from vibearr_runner import processes
        from vibearr_runner.delivery import SupervisorGit
        from vibearr_runner.native_delivery import NativeDelivery

        self.advance("delivery")
        transport = SupervisorGit(token="fixture-token")
        paused = []
        original = processes.run

        def run(arguments, **kwargs):
            if "push" in arguments:
                paused.append(True)
            return original(arguments, **kwargs)

        self.host.checkpoint = lambda: not paused
        with patch.object(
            NativeDelivery, "clients", return_value=(GitHub(), transport)
        ):
            with patch.object(transport, "observed_remote", return_value=""):
                with patch.object(processes, "run", side_effect=run):
                    self.assertEqual(self.engine().step(self.state), "wait")
                self.state = self.journal.load()
                self.assertFalse(
                    self.state["native"]["delivery"].get("push_dispatched")
                )
                self.state["run"]["run"]["cancellation_requested_at"] = datetime.now(
                    timezone.utc
                ).isoformat()
                self.assertEqual(self.engine().step(self.state), "reporting_pending")
        self.assertEqual(len(paused), 1)
        self.assertEqual(len(self.queue.finishes), 1)
        self.assertEqual(self.queue.finishes[0]["outcome"], "canceled")

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

    def test_confirmed_expired_session_without_export_finishes_failed(self):
        self.advance("agent")
        self.engine().step(self.state)
        operation = self.executor.started[0]
        result = self.executor.states[operation]["result"]
        result.update(exit_code=None, timed_out=True, canceled=True)
        result.pop("artifact")
        self.engine().step(self.state)
        self.assertEqual(self.queue.finishes[0]["outcome"], "failed")
        self.assertEqual(self.state["native"]["phase"], "agent")
        self.assertEqual(
            git(["rev-list", "--count", "HEAD"], self.workspace.checkout), "1"
        )

    def test_default_branch_change_stops_before_importing_the_candidate(self):
        self.advance("import_candidate")
        self.queue.default_branch = "next"
        self.state["native"]["source_checked_at"] = 0
        self.engine().step(self.state)
        self.assertEqual(self.queue.finishes[0]["outcome"], "failed")
        self.assertIn("default branch changed", self.queue.finishes[0]["error_summary"])

    def test_lost_prepare_response_reuses_the_frozen_prompt_after_supervisor_upgrade(
        self,
    ):
        from vibearr_executor.client import ExecutorUnavailable

        self.advance("agent")
        original = self.executor.prepare
        lost = False

        def prepare(manifest):
            nonlocal lost
            result = original(manifest)
            if not lost and manifest["profile_id"] == self.executor.agent.profile_id:
                lost = True
                raise ExecutorUnavailable("Fixture lost the prepared response")
            return result

        with patch.object(self.executor, "prepare", side_effect=prepare):
            with self.assertRaises(ReconciliationRequired):
                self.engine().step(self.state)
            frozen = self.state["native"]["agent"]["request"]["prompt"]
            with patch.object(
                NativeLifecycle,
                "agent_prompt",
                return_value="Changed supervisor header",
            ) as prompt:
                self.assertEqual(self.engine().step(self.state), "running")
                prompt.assert_not_called()
        self.assertEqual(self.state["native"]["agent"]["request"]["prompt"], frozen)
        self.assertEqual(len(self.executor.started), 1)
