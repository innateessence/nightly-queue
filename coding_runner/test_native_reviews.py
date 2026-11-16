from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

from coding_runner.test_automatic_merge import MergeHost, profile
from coding_runner.test_native_contract import observed_operation
from vibearr_runner.delivery import DeliveryError, DeliveryUnknown
from vibearr_runner.merge_github import MergeRepair
from vibearr_runner.native_codex import CodexEvents
from vibearr_runner.native_contract import intent_operations
from vibearr_runner.native_reviews import (
    NativeReviews,
    model_findings,
    native_review_result,
)
from vibearr_runner.native_sessions import NativeSession
from vibearr_runner.native_validation import CandidateSource
from vibearr_runner.recovery import ReconciliationRequired
from vibearr_runner.review_publication import ReviewPublication
from vibearr_runner.workspaces import Workspace


SDK_AVAILABLE = importlib.util.find_spec("vibearr_executor") is not None


def final_event(findings=None):
    return {
        "kind": "item.completed",
        "item_type": "agent_message",
        "text": json.dumps({"complete": True, "findings": findings or []}),
    }


class ModelReviewOutputTests(unittest.TestCase):
    def test_missing_explicit_result_is_not_no_findings(self):
        with self.assertRaises(ValueError):
            model_findings([])

    def test_truncated_final_json_cannot_authorize_review(self):
        with self.assertRaises(ValueError):
            model_findings([{**final_event(), "text_truncated": True}])

    def test_incomplete_model_review_is_rejected(self):
        with self.assertRaises(ValueError):
            model_findings(
                [{**final_event(), "text": '{"complete":false,"findings":[]}'}]
            )

    def test_native_parser_retains_truncated_message_fact(self):
        parser = CodexEvents()
        parser.feed(
            (
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {"type": "agent_message", "text": "x" * 4001},
                    }
                )
                + "\n"
            ).encode(),
            final=True,
        )
        self.assertTrue(parser.events[0]["text_truncated"])
        self.assertEqual(len(parser.events[0]["text"]), 4000)


class ReviewStatusRecoveryTests(unittest.TestCase):
    def test_canceled_unknown_status_reconciles_without_a_second_write(self):
        host = MergeHost()
        configured = profile()
        report_commit = "e" * 40
        publication_id = str(uuid4())
        control_pull = {
            "repository_id": 17,
            "number": 20,
            "url": "https://github.com/owner/repo/pull/20",
            "base": "vibearr/reviews",
            "head": "vibearr/review-run/task",
            "head_sha": report_commit,
        }
        binding = {
            "publication_id": publication_id,
            "workspace_id": host.native["workspace_id"],
            "workspace": host.native["candidate"]["workspace"],
            "pull_request": host.native["delivery"]["pull_request"],
            "review_ids": [str(uuid4()), str(uuid4())],
            "repository_id": 17,
            "repository": "owner/repo",
            "control_base": "vibearr/reviews",
            "base_revision": "a" * 40,
            "branch": control_pull["head"],
            "commit_sha": report_commit,
            "files": [
                {"path": f"reports/{role}{suffix}", "digest": "a" * 64}
                for role in ("independent", "root")
                for suffix in (".json", ".receipt.json")
            ],
            "report_paths": {
                role: f"reports/{role}.json" for role in ("independent", "root")
            },
        }
        reviews = {
            "publication": {
                "publication_id": publication_id,
                "directory": "/tmp/unused-native-review-status-fixture",
                "binding": binding,
                "profile": configured,
                "pull_request": control_pull,
                "facts": {"push": {}, "create_pr": {}},
            }
        }
        observed, writes = [], []

        def request(method, path, payload=None):
            writes.append((method, path))
            observed.append({**payload, "id": 50, "creator": {"id": 25}})
            raise DeliveryUnknown("Actual status created; response lost")

        github = SimpleNamespace(pages=lambda *args: observed, request=request)
        publication = ReviewPublication(host, reviews)
        with patch.object(
            ReviewPublication, "clients", return_value=(github, object())
        ):
            self.assertEqual(publication.step(configured), "continue")
            publication.terminal()
        self.assertEqual(len(writes), 1)
        self.assertEqual(
            publication.data["facts"]["status:independent"]["status"]["id"], 50
        )
        self.assertEqual(publication.data["binding"]["control_base"], "vibearr/reviews")

    def test_missing_dispatched_status_keeps_cancellation_fenced(self):
        host = MergeHost()
        data = {
            "publication": {
                "publication_id": str(uuid4()),
                "binding": {
                    "repository": "owner/repo",
                    "commit_sha": "e" * 40,
                    "workspace": host.native["candidate"]["workspace"],
                    "report_paths": {"independent": "reports/independent.json"},
                },
                "profile": profile(),
                "intents": {"status:independent": str(uuid4())},
                "status:independent:dispatched": True,
            }
        }
        github = SimpleNamespace(pages=lambda *args: [])
        with patch.object(
            ReviewPublication, "clients", return_value=(github, object())
        ):
            with self.assertRaises(ReconciliationRequired):
                ReviewPublication(host, data).terminal()
        self.assertEqual(host.records, [])


class Adapter:
    review_purpose_supported = True

    def __init__(self):
        self.prepared = []
        self.sessions = {}
        self.states = {}
        self.findings = {}

    def prepare(self, **request):
        self.prepared.append(request["operation_id"])
        session = NativeSession(
            "codex",
            "test",
            {
                "backend": "codex",
                "credential_id": 1,
                "credential_version": 1,
                "model": "test",
            },
            "token",
            "sha256:" + "a" * 64,
            "b" * 64,
            {
                "schema_version": 1,
                "run_id": request["run_id"],
                "operation_id": request["operation_id"],
                "profile_id": "native-default",
                "profile_digest": "c" * 64,
                "source_digest": hashlib.sha256(request["source"]).hexdigest(),
                "context_digest": "d" * 64,
                "phase": "agent",
                "limits": {
                    "cpu": 2,
                    "memory_mb": 4096,
                    "pids": 128,
                    "workspace_mb": 2048,
                },
                "deadline": request["deadline"],
                "network_hosts": ["api.openai.com"],
            },
            purpose=request.get("purpose", "coding"),
        )
        self.sessions[request["operation_id"]] = session
        self.states[request["operation_id"]] = "not-started"
        return session

    def start(self, session):
        self.states[session.manifest["operation_id"]] = "running"

    def reconcile(self, session):
        operation = session.manifest["operation_id"]
        state = self.states[operation]
        if state != "exited":
            return {"state": state, "operation_id": operation}
        from vibearr_executor.artifacts import pack, result_manifest

        source = pack({"file.py": (0o644, b"before\n")})
        return {
            "state": "exited",
            "operation_id": operation,
            "descendants_terminated": True,
            "container_id": hashlib.sha256(operation.encode()).hexdigest(),
            "result": {
                "schema_version": 1,
                "run_id": session.manifest["run_id"],
                "operation_id": operation,
                "terminated": True,
                "exit_code": 0,
                "timed_out": False,
                "canceled": False,
                "log_truncated": False,
                "started_at": 1700000000.0,
                "ended_at": 1700000001.0,
                "log_digest": hashlib.sha256(b"events").hexdigest(),
                "artifact": result_manifest(source, source),
            },
        }

    def outcome(self, session, source):
        observed = self.reconcile(session)
        operation = session.manifest["operation_id"]
        return {
            "result": observed["result"],
            "artifact": observed["result"]["artifact"],
            "source": source,
            "successful": True,
            "session_id": "session-" + operation,
            "agent_version": "test",
            "usage": {"input": 12, "output": 8},
            "usage_unit": "tokens",
            "events": [final_event(self.findings.get(operation))],
        }


@unittest.skipUnless(
    SDK_AVAILABLE, "Install the native-runtime extra for protected review contracts"
)
class NativeReviewOwnershipTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.host = MergeHost()
        from vibearr_executor.artifacts import pack

        self.source = CandidateSource(
            "b" * 40, "c" * 40, pack({"file.py": (0o644, b"before\n")})
        )
        self.host.native["candidate"]["source_digest"] = self.source.digest
        self.host.native["ci_deadline"] = (
            datetime.now(timezone.utc) + timedelta(minutes=30)
        ).isoformat()
        self.host.native["context"]["document"]["acceptance_criteria"] = [
            "Preserve behavior."
        ]
        self.host.snapshot["effective"].update(
            implementation_backend="codex", implementation_model="test"
        )
        self.adapter = Adapter()
        self.host.registry = SimpleNamespace(create=lambda *args: self.adapter)
        self.host.executor = object()
        operations = {}
        self.host.resources = SimpleNamespace(
            reserve=lambda *args: None,
            operation=lambda run, manifest, **kwargs: operations.update(
                {manifest["operation_id"]: manifest}
            ),
            dispatch=lambda *args: None,
        )
        self.operations = operations
        self.host.workspace = lambda: Workspace(
            self.host.run_id,
            "https://github.com/owner/repo.git",
            "main",
            "main",
            "a" * 40,
            "task/1",
            self.directory,
        )
        self.host.secret = lambda *args: {
            "credential": {"id": 1, "version": 1},
            "material": {"secret": "fixture"},
        }
        self.host.native["intents"] = {}
        self.host.state = {"native": self.host.native}

        def intent(payload, key):
            identity = str(uuid4())
            previous = next(
                (value for value in self.host.records if value[0] == key), None
            )
            if previous:
                return previous[2]
            self.host.records.append((key, payload, identity))
            self.host.native["intents"][identity] = payload
            return identity

        self.host.intent = intent
        self.host.artifact = lambda *args: str(uuid4())
        source = patch.object(CandidateSource, "from_git", return_value=self.source)
        source.start()
        self.addCleanup(source.stop)
        prompt = patch.object(
            NativeReviews, "prompt", return_value=("Trusted review prompt", "e" * 64)
        )
        prompt.start()
        self.addCleanup(prompt.stop)

    def test_restart_reconciles_same_paid_review_before_starting_root(self):
        original_deadline = self.host.native["deadline"]
        self.assertEqual(NativeReviews(self.host).step(profile()), "running")
        first = self.adapter.prepared[0]
        self.assertEqual(NativeReviews(self.host).step(profile()), "running")
        self.assertEqual(self.adapter.prepared, [first])
        self.adapter.states[first] = "exited"
        self.assertEqual(NativeReviews(self.host).step(profile()), "continue")
        self.assertEqual(NativeReviews(self.host).step(profile()), "running")
        second = self.adapter.prepared[1]
        self.assertNotEqual(first, second)
        self.adapter.states[second] = "exited"
        self.assertEqual(NativeReviews(self.host).step(profile()), "continue")
        results = self.host.native["reviews"]["b" * 40]["roles"]
        self.assertNotEqual(
            results["independent"]["result"]["session_id"],
            results["root"]["result"]["session_id"],
        )
        self.assertEqual(self.operations[first]["run_id"], self.host.run_id)
        self.assertEqual(self.operations[second]["run_id"], self.host.run_id)
        self.assertEqual(
            self.operations[first]["deadline"], self.operations[second]["deadline"]
        )
        self.assertEqual(self.host.native["attempt"], 0)
        self.assertEqual(self.host.native["deadline"], original_deadline)
        self.assertEqual(
            set(self.operations),
            {
                operation
                for value in self.host.native["intents"].values()
                for operation in intent_operations(value)
            },
        )

    def test_unknown_review_never_prepares_replacement(self):
        NativeReviews(self.host).step(profile())
        operation = self.adapter.prepared[0]
        self.adapter.states[operation] = "unknown"
        with self.assertRaises(ReconciliationRequired):
            NativeReviews(self.host).step(profile())
        self.assertEqual(self.adapter.prepared, [operation])

    def test_p2_findings_preserve_terminal_review_and_request_bounded_repair(self):
        NativeReviews(self.host).step(profile())
        operation = self.adapter.prepared[0]
        self.adapter.findings[operation] = [
            {"priority": "P2", "description": "file.py:1 returns the wrong value."}
        ]
        self.adapter.states[operation] = "exited"
        NativeReviews(self.host).step(profile())
        with self.assertRaisesRegex(MergeRepair, "file.py:1"):
            NativeReviews(self.host).step(profile())
        self.assertEqual(len(self.adapter.prepared), 1)
        self.assertEqual(self.host.native["attempt"], 0)

    def test_expired_review_deadline_prevents_paid_start(self):
        self.host.native["review_deadline"] = "2000-01-01T00:00:00+00:00"
        with self.assertRaises(DeliveryError):
            NativeReviews(self.host).step(profile())
        self.assertEqual(self.adapter.prepared, [])

    def test_changed_export_cannot_claim_passing_review(self):
        original, changed, observed = observed_operation()
        session = NativeSession(
            "codex",
            "test",
            {
                "backend": "codex",
                "credential_id": 1,
                "credential_version": 1,
                "model": "test",
            },
            "token",
            "sha256:" + "a" * 64,
            "b" * 64,
            {
                "run_id": observed["result"]["run_id"],
                "operation_id": observed["operation_id"],
            },
        )
        outcome = {
            "result": observed["result"],
            "artifact": observed["result"]["artifact"],
            "source": changed,
            "successful": True,
            "session_id": "real-session",
            "agent_version": "test",
            "events": [final_event()],
        }
        value = native_review_result(
            intent_id=str(uuid4()),
            role="independent",
            session=session,
            observed=observed,
            outcome=outcome,
            workspace=self.host.native["candidate"]["workspace"],
            pull=self.host.native["delivery"]["pull_request"],
            context_digest="d" * 64,
        )
        self.assertFalse(value["successful"])
        self.assertIsNone(value["report"])
        self.assertNotEqual(
            hashlib.sha256(original).hexdigest(), hashlib.sha256(changed).hexdigest()
        )


if __name__ == "__main__":
    unittest.main()
