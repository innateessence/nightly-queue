import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
import zipfile
from urllib.error import HTTPError
from unittest.mock import Mock, patch

from .backends import BackendOutputError
from .journal import Journal
from .qualification_trace import ModelTraceTransport
from .release_evidence import verify_boundary_workflow
from .test_backends import response


class WorkflowEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.boundary = {
            "workflow_run_url": "https://github.com/example/app/actions/runs/17",
            "environment": {
                "source_revision": "a" * 40,
                "image_digest": "sha256:" + "b" * 64,
            },
            "code_revision": "c" * 64,
            "environment_digest": "d" * 64,
            "command": ["python", "-m", "coding_runner.verify", "--boundary"],
            "exit_code": 0,
            "output_digest": "e" * 64,
        }
        self.run = {
            "status": "completed",
            "conclusion": "success",
            "event": "workflow_dispatch",
            "path": ".github/workflows/runner-deployment-boundary.yml",
            "head_sha": "a" * 40,
        }
        self.artifacts = {
            "total_count": 1,
            "artifacts": [
                {"name": "runner-deployment-boundary", "expired": False, "id": 19}
            ],
        }
        self.document = {**self.boundary, "tests_run": 50}
        self.opener = Mock()
        self.patch = patch(
            "coding_runner.release_evidence.build_opener", return_value=self.opener
        )
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def archive(self, document=None):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(
                "runner-boundary.json", json.dumps(document or self.document)
            )
        return buffer.getvalue()

    def responses(
        self,
        *,
        redirect="https://fixture.blob.core.windows.net/signed?key=value",
        archive=None,
    ):
        self.opener.open.side_effect = [
            io.BytesIO(json.dumps(self.run).encode()),
            io.BytesIO(json.dumps(self.artifacts).encode()),
            HTTPError(
                "https://api.github.com/artifact",
                302,
                "Found",
                {"Location": redirect},
                None,
            ),
            io.BytesIO(archive or self.archive()),
        ]

    def test_successful_run_downloads_and_verifies_bound_artifact(self):
        self.responses()
        evidence = verify_boundary_workflow(self.boundary, "private-fixture-token")
        self.assertEqual(evidence["artifact_id"], 19)
        signed_request = self.opener.open.call_args_list[-1].args[0]
        self.assertNotIn("Authorization", signed_request.headers)
        self.assertNotIn("private-fixture-token", signed_request.full_url)

    def test_foreign_artifact_redirect_is_rejected_before_download(self):
        self.responses(redirect="https://attacker.example/archive")
        with self.assertRaisesRegex(ValueError, "download host"):
            verify_boundary_workflow(self.boundary, "private-fixture-token")
        self.assertEqual(self.opener.open.call_count, 3)

    def test_failed_workflow_cannot_qualify(self):
        self.run["conclusion"] = "failure"
        self.responses()
        with self.assertRaisesRegex(ValueError, "failed"):
            verify_boundary_workflow(self.boundary, "fixture-token")
        self.assertEqual(self.opener.open.call_count, 1)

    def test_changed_source_revision_is_rejected(self):
        self.run["head_sha"] = "f" * 40
        self.responses()
        with self.assertRaises(ValueError):
            verify_boundary_workflow(self.boundary, "fixture-token")

    def test_changed_code_digest_is_rejected(self):
        self.document["code_revision"] = "f" * 64
        self.responses()
        with self.assertRaisesRegex(ValueError, "does not match"):
            verify_boundary_workflow(self.boundary, "fixture-token")

    def test_changed_environment_is_rejected(self):
        self.document["environment"] = {"source_revision": "f" * 40}
        self.responses()
        with self.assertRaisesRegex(ValueError, "does not match"):
            verify_boundary_workflow(self.boundary, "fixture-token")

    def test_compressed_oversize_json_is_rejected_before_extraction(self):
        self.document["padding"] = "x" * 70000
        self.responses()
        with self.assertRaisesRegex(ValueError, "exceeds its limit"):
            verify_boundary_workflow(self.boundary, "fixture-token")

    def test_archive_command_text_is_never_executed(self):
        self.document["command"] = ["touch", "/tmp/untrusted-artifact-command"]
        self.responses()
        with self.assertRaisesRegex(ValueError, "does not match"):
            verify_boundary_workflow(self.boundary, "fixture-token")


class ModelTraceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.journal = Journal(Path(temporary.name) / "journal")
        self.journal.__enter__()
        self.addCleanup(self.journal.__exit__)
        self.state = self.journal.load()
        self.journal.save(self.state)
        self.original = self.journal.path.read_bytes()
        self.account = {
            "backend": "codex",
            "credential_id": 7,
            "model": "fixture-model",
        }
        self.token = "selected-fixture-credential"

    def exchange(self, value, stage="research"):
        transport = ModelTraceTransport(
            self.journal, self.state, self.account, 2, stage
        )
        script = (
            "import json,sys; json.load(sys.stdin); print(json.dumps("
            + repr(value)
            + "))"
        )
        transport.command = lambda: [sys.executable, "-I", "-c", script]
        return transport.exchange(
            {"token": self.token, "request": {"model": self.account["model"]}},
            timeout=3,
            checkpoint=lambda: True,
            stop=threading.Event(),
        )

    def assert_no_trace(self):
        self.assertNotIn("release_evidence", self.state)
        self.assertNotIn("release_evidence", self.journal.load())
        self.assertNotIn(self.token, self.journal.path.read_text())
        self.assertEqual(self.journal.path.read_bytes(), self.original)

    def test_secret_response_id_is_rejected_before_journal_persistence(self):
        value = response()
        value["response"]["id"] = "provider-echo-" + self.token
        with self.assertRaises(BackendOutputError):
            self.exchange(value)
        self.assert_no_trace()

    def test_secret_proposal_is_rejected_before_journal_persistence(self):
        value = response("complete", {"summary": "credential echo: " + self.token})
        value["response"]["id"] = "resp_fixture"
        with self.assertRaises(BackendOutputError):
            self.exchange(value, stage="implementation")
        self.assert_no_trace()

    def test_valid_implementation_response_persists_exact_trace(self):
        value = response("complete", {"summary": "Finished the fixture change"})
        value["response"]["id"] = "resp_fixture"
        self.assertEqual(self.exchange(value, stage="implementation"), value)
        traces = self.journal.load()["release_evidence"]["model_responses"]
        self.assertEqual(len(traces), 1)
        self.assertEqual(traces[0]["response_id"], "resp_fixture")
        self.assertEqual(traces[0]["secret_version"], 2)
        self.assertEqual(traces[0]["model"], "fixture-model")

    def test_native_tool_response_cannot_enter_durable_trace(self):
        value = response()
        value["response"].update(id="resp_fixture", output=[{"type": "function_call"}])
        with self.assertRaises(BackendOutputError):
            self.exchange(value)
        self.assert_no_trace()
