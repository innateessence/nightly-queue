from copy import deepcopy
from datetime import datetime, timedelta, timezone
import importlib.util
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

from vibearr_runner.journal import Journal
from vibearr_runner.native_preflight import NativePreflight
from vibearr_runner.native_sessions import NativeSession
from vibearr_runner.policy import default_execution_policy
from vibearr_runner.recovery import ReconciliationRequired


class Executor:
    def __init__(self):
        self.instance = str(uuid4())
        self.stopped = []
        self.manifests = {}

    def capabilities(self):
        return {"instance_id": self.instance, "binding_digest": "b" * 64}

    def stop(self, operation):
        self.stopped.append(operation)
        return {"state": "exited", "descendants_terminated": True}

    def prepare(self, manifest):
        self.manifests[manifest["operation_id"]] = manifest
        return {}

    def termination_receipt(self, operation):
        manifest = self.manifests[operation]
        return {
            "schema_version": 1,
            "instance_id": self.instance,
            "run_id": manifest["run_id"],
            "operation_id": operation,
            "manifest_digest": hashlib.sha256(
                json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
            "state": "not-started",
            "resources_terminated": False,
        }

    def status(self, operation):
        return {"state": "running"}

    def pause(self, operation):
        return {}

    def resume(self, operation):
        return {}


class Adapter:
    def __init__(self, journal):
        self.journal = journal
        self.state = "not-started"
        self.starts = []
        self.prepared = []
        self.terminated = True

    def diagnostics(self, policy, grant, *, model, proof=None):
        return {
            "auth_available": True,
            "native_agent_ready": bool(proof),
            "reason": "fixture",
        }

    def prepare_auth_probe(self, **request):
        self.prepared.append(request["operation_id"])
        account = {
            "backend": "codex",
            "credential_id": 1,
            "credential_version": 2,
            "model": request["model"],
        }
        manifest = {
            "run_id": request["run_id"],
            "operation_id": request["operation_id"],
            "source_digest": "a" * 64,
            "context_digest": "c" * 64,
            "profile_digest": "d" * 64,
        }
        return NativeSession(
            "codex", "test", account, "token", "sha256:" + "a" * 64, "b" * 64, manifest
        )

    def reconcile(self, session):
        return {"state": self.state, "descendants_terminated": self.terminated}

    def start(self, session):
        saved = self.journal.load()["native_preflight"]["accounts"]
        assert any(
            entry.get("session") == session.to_data() and entry.get("start_intent")
            for entry in saved.values()
        )
        if session.manifest["operation_id"] not in self.starts:
            self.starts.append(session.manifest["operation_id"])
        self.state = "running"

    def authentication_result(self, session):
        return {"account": session.account, "authenticated": True}


class Queue:
    def __init__(self):
        self.candidates = [1, 2]
        self.calls = []

    def request(self, method, path, payload=None):
        self.calls.append((method, path, payload))
        if path.endswith("/candidates/"):
            return {
                "configuration_revision": "revision",
                "project_ids": self.candidates,
            }
        return {
            "credential": {"id": 1, "version": 2},
            "material": {"secret": "fixture"},
        }


@unittest.skipUnless(
    importlib.util.find_spec("vibearr_executor") is not None,
    "Install the native-runtime extra for native preflight tests",
)
class NativePreflightTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.journal = Journal(Path(temporary.name) / "journal")
        self.journal.__enter__()
        self.addCleanup(self.journal.__exit__)
        self.state = self.journal.load()
        self.executor, self.queue = Executor(), Queue()
        self.adapter = Adapter(self.journal)
        original_prepare = self.adapter.prepare_auth_probe

        def prepare(**request):
            session = original_prepare(**request)
            self.executor.prepare(session.manifest)
            return session

        self.adapter.prepare_auth_probe = prepare
        registry = SimpleNamespace(create=lambda provider, executor: self.adapter)
        self.preflight = NativePreflight(
            self.queue,
            self.executor,
            self.journal,
            registry=registry,
            profile=object(),
            github_check=lambda grant, project: {"read_verified": True},
        )
        self.configuration = {
            "revision": "revision",
            "credentials": [{"id": 1, "secret_version": 2}],
            "projects": [],
        }
        for identity in (1, 2):
            self.configuration["projects"].append(
                {
                    "project_id": identity,
                    "execution_policy": default_execution_policy(),
                    "effective": {
                        "implementation_backend": "codex",
                        "implementation_model": "model",
                        "implementation_credential_id": 1,
                        "repository_credential_id": 3,
                        "issue_credential_id": 4,
                    },
                }
            )
        patcher = patch(
            "vibearr_runner.native_validation.validation_diagnostics",
            return_value={"ready": True},
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_idle_queue_makes_no_secret_requests_or_paid_probe(self):
        self.queue.candidates = []
        self.assertEqual(self.preflight.select(self.configuration, self.state), [])
        self.assertEqual(self.adapter.prepared, [])
        self.assertEqual(len(self.queue.calls), 1)

    def test_merge_policy_is_rejected_before_secrets_or_paid_probe(self):
        for project in self.configuration["projects"]:
            project["execution_policy"]["delivery_mode"] = "merge"
        self.assertEqual(self.preflight.select(self.configuration, self.state), [])
        self.assertEqual(self.adapter.prepared, [])
        self.assertEqual(self.adapter.starts, [])
        self.assertEqual(len(self.queue.calls), 1)
        for report in self.state["native_preflight"]["projects"].values():
            self.assertIn("human", report["reason"])

    def test_eligible_projects_share_one_journaled_authentication_operation(self):
        self.assertEqual(self.preflight.select(self.configuration, self.state), [])
        self.assertEqual(len(self.adapter.starts), 1)
        self.adapter.state = "exited"
        self.assertEqual(self.preflight.select(self.configuration, self.state), [1, 2])
        self.assertEqual(len(self.adapter.prepared), 1)
        self.assertEqual(len(self.adapter.starts), 1)

    def test_unknown_probe_after_restart_keeps_the_original_operation(self):
        self.preflight.select(self.configuration, self.state)
        original = deepcopy(self.state["native_preflight"])
        self.adapter.state = "unknown"
        recovered = self.journal.load()
        self.assertEqual(self.preflight.select(self.configuration, recovered), [])
        self.assertEqual(self.adapter.prepared, self.adapter.starts)
        self.assertEqual(
            set(recovered["native_preflight"]["accounts"]), set(original["accounts"])
        )

    def test_rotation_stops_pending_authentication_instead_of_starting_another(self):
        self.preflight.select(self.configuration, self.state)
        self.configuration["credentials"][0]["secret_version"] = 3
        self.queue.candidates = []
        self.preflight.select(self.configuration, self.state)
        self.assertEqual(self.executor.stopped, self.adapter.starts)
        self.assertEqual(len(self.adapter.starts), 1)

    def test_executor_storage_reset_fences_preflight_recovery(self):
        self.preflight.select(self.configuration, self.state)
        self.executor.instance = str(uuid4())
        with self.assertRaises(ReconciliationRequired):
            self.preflight.select(self.configuration, self.state)
        self.assertEqual(len(self.adapter.starts), 1)

    def test_expired_unprepared_probe_does_not_create_a_new_identity(self):
        request = {
            "run_id": str(uuid4()),
            "operation_id": str(uuid4()),
            "deadline": (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
        }
        self.state["native_preflight"] = {
            "accounts": {"old": {"pending": True, "request": request}},
            "projects": {},
        }
        self.queue.candidates = []
        self.preflight.select(self.configuration, self.state)
        self.assertEqual(self.executor.stopped, [request["operation_id"]])
        self.assertEqual(self.adapter.prepared, [])

    def test_exited_probe_without_descendant_proof_keeps_the_original_fence(self):
        self.preflight.select(self.configuration, self.state)
        self.adapter.state = "exited"
        self.adapter.terminated = False
        self.assertEqual(self.preflight.select(self.configuration, self.state), [])
        self.assertTrue(self.preflight.pending_accounts(self.state["native_preflight"]))
        self.assertEqual(len(self.adapter.starts), 1)
