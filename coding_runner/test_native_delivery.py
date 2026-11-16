from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

from vibearr_runner.client import AmbiguousRequest
from vibearr_runner.delivery import DeliveryUnknown
from vibearr_runner.native_delivery import NativeDelivery
from vibearr_runner.native_sessions import NativeSession
from vibearr_runner.policy import ExecutionPolicy
from vibearr_runner.recovery import ReconciliationRequired
from vibearr_runner.workspaces import Workspace


class GitHub:
    def __init__(self):
        self.pull_request = None
        self.creates = 0
        self.lose_create = False
        self.repository_id = 17

    def repository(self, **kwargs):
        return {
            "repository_id": self.repository_id,
            "repository": "owner/repo",
            "repository_url": "https://github.com/owner/repo.git",
            "default_branch": "main",
        }, {}

    def find_native(self, *arguments):
        return self.pull_request

    def create_native(self, identity, repository, branch, base, commit, title, body):
        self.creates += 1
        self.pull_request = {
            "repository_id": identity,
            "number": 19,
            "url": f"https://github.com/{repository}/pull/19",
            "base": base,
            "head": branch,
            "head_sha": commit,
        }
        if self.lose_create:
            self.lose_create = False
            raise DeliveryUnknown("lost PR response")
        return self.pull_request


class Transport:
    def __init__(self):
        self.remote = ""
        self.pushes = 0
        self.lose_push = False

    def observed_remote(self, workspace):
        return self.remote

    def push(self, workspace, commit):
        self.pushes += 1
        self.remote = commit
        if self.lose_push:
            self.lose_push = False
            raise DeliveryUnknown("lost push response")


class Host:
    def __init__(self):
        self.run_id = str(uuid4())
        self.token = str(uuid4())
        self.workspace_value = Workspace(
            self.run_id,
            "https://github.com/owner/repo.git",
            "main",
            "main",
            "a" * 40,
            "task/native",
            Path("/tmp/unused-native-delivery-fixture"),
        )
        self.workspace_id = str(uuid4())
        self.policy = ExecutionPolicy()
        self.state = {"runner_id": 1}
        self.run = {"run": {"configuration_revision": "c" * 64}}
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
            "sha256:" + "d" * 64,
            "e" * 64,
            {
                "run_id": self.run_id,
                "operation_id": str(uuid4()),
                "source_digest": "f" * 64,
                "context_digest": "a" * 64,
                "profile_digest": "b" * 64,
                "profile_id": "agent-v1",
            },
        )
        workspace = {
            "repository_id": 17,
            "repository": "owner/repo",
            "branch": "task/native",
            "base_branch": "main",
            "base_revision": "a" * 40,
            "tree_sha": "b" * 40,
            "commit_sha": "c" * 40,
        }
        self.native = {
            "attempt": 0,
            "workspace_id": self.workspace_id,
            "candidate": {"workspace": workspace},
            "repository": {"repository_url": self.workspace_value.repository_url},
            "context": {
                "id": str(uuid4()),
                "digest": "a" * 64,
                "document": {
                    "repository": {"base_branch": "main"},
                    "source": {"title": "Native fixture"},
                },
            },
            "agent": {
                "session": session.to_data(),
                "session_id": "provider-session",
                "agent_version": "test",
            },
            "validation": {
                "evidence": {"tree_sha": "b" * 40, "checks": [{"id": "fixture"}]}
            },
            "intents": {},
        }
        self.records = {}
        self.finishes = 0
        self.fail_finish = False
        self.client = SimpleNamespace(
            request=self.request,
            reconcile=lambda identity: {"run": {"state": "active"}},
        )

    def save(self):
        pass

    def context_binding(self):
        return {
            "context_id": self.native["context"]["id"],
            "context_digest": "a" * 64,
            "policy_digest": self.policy.digest,
            "attempt": 0,
        }

    def intent(self, payload, key):
        identity = self.artifact("native_intent", payload, key)
        self.native["intents"][identity] = payload
        return identity

    def artifact(self, kind, payload, key):
        if key in self.records:
            assert self.records[key][1] == payload
        else:
            self.records[key] = (str(uuid4()), deepcopy(payload))
        return self.records[key][0]

    def workspace(self):
        return self.workspace_value

    def checkpoint(self, **kwargs):
        return True

    def status(self, *args, **kwargs):
        pass

    def terminate(self):
        pass

    def request(self, method, path, payload):
        self.finishes += 1
        if self.fail_finish:
            self.fail_finish = False
            raise AmbiguousRequest("lost finish response")
        return {}


class NativePublicationTests(unittest.TestCase):
    def setUp(self):
        self.host, self.github, self.git = Host(), GitHub(), Transport()
        clients = patch.object(
            NativeDelivery, "clients", return_value=(self.github, self.git)
        )
        verify = patch.object(NativeDelivery, "verify")
        clients.start()
        verify.start()
        self.addCleanup(clients.stop)
        self.addCleanup(verify.stop)

    def complete(self):
        for _ in range(8):
            result = NativeDelivery(self.host).step()
            if result == "reporting_pending":
                return result
        self.fail("Publication did not reach completion reporting")

    def test_lost_push_response_reconciles_the_same_ref_without_another_push(self):
        self.git.lose_push = True
        with self.assertRaises(ReconciliationRequired):
            self.complete()
        publication = self.host.native["delivery"]["publication_id"]
        self.assertEqual(self.complete(), "reporting_pending")
        self.assertEqual(self.git.pushes, 1)
        self.assertEqual(self.github.creates, 1)
        self.assertEqual(self.host.native["delivery"]["publication_id"], publication)

    def test_lost_pr_response_reconciles_the_same_pr_without_another_create(self):
        self.github.lose_create = True
        with self.assertRaises(ReconciliationRequired):
            self.complete()
        self.assertEqual(self.complete(), "reporting_pending")
        self.assertEqual(self.github.creates, 1)
        self.assertEqual(self.git.pushes, 1)

    def test_invisible_unknown_pr_keeps_claim_ownership(self):
        self.github.lose_create = True
        with self.assertRaises(ReconciliationRequired):
            self.complete()
        self.github.pull_request = None
        with self.assertRaises(ReconciliationRequired):
            self.complete()
        with self.assertRaises(ReconciliationRequired):
            NativeDelivery(self.host).reconcile_publications()
        self.assertEqual(self.github.creates, 1)
        self.assertEqual(self.host.finishes, 0)

    def test_cancel_after_lost_pr_response_records_provider_facts_before_termination(
        self,
    ):
        self.github.lose_create = True
        with self.assertRaises(ReconciliationRequired):
            self.complete()
        result = NativeDelivery(self.host).reconcile_publications()
        self.assertTrue(all(value["state"] == "known_pr" for value in result))
        fact = self.host.records["publication-fact:pr"][1]
        self.assertEqual(fact["pull_request"], self.github.pull_request)
        self.assertEqual(self.github.creates, 1)

    def test_lost_finish_response_does_not_repeat_publication(self):
        self.host.fail_finish = True
        with self.assertRaises(AmbiguousRequest):
            self.complete()
        self.assertEqual(self.complete(), "reporting_pending")
        self.assertEqual(self.github.creates, 1)
        self.assertEqual(self.git.pushes, 1)

    def test_existing_unowned_pull_request_is_not_adopted(self):
        self.github.create_native(
            17, "owner/repo", "task/native", "main", "c" * 40, "External", ""
        )
        with self.assertRaisesRegex(ReconciliationRequired, "unowned"):
            self.complete()
        self.assertEqual(self.host.finishes, 0)

    def test_explicit_base_branch_can_differ_from_the_repository_default(self):
        self.host.native["repository_at_checkout"] = {"default_branch": "main"}
        self.host.native["candidate"]["workspace"]["base_branch"] = "release"
        self.host.native["context"]["document"]["repository"]["base_branch"] = "release"
        self.host.workspace_value = replace(
            self.host.workspace_value, resolved_base_branch="release"
        )
        self.assertEqual(self.complete(), "reporting_pending")
        self.assertEqual(self.github.pull_request["base"], "release")
