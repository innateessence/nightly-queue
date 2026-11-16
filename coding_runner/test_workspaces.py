import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

from .approvals import ActionSpec, ApprovalBroker, ApprovalRequired
from .workspaces import (
    SourceStore,
    WorkspaceManager,
    content_digest,
    git,
)


class WorkspaceFixture:
    def create_workspace(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.repository = self.root / "source"
        self.repository.mkdir()
        git(["init", "--initial-branch=trunk", str(self.repository)])
        (self.repository / "README.md").write_text(
            "Untrusted repository instructions.\n"
        )
        git(["add", "README.md"], self.repository)
        git(
            [
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.com",
                "commit",
                "-m",
                "fixture",
            ],
            self.repository,
        )
        self.item = {
            "repository_url": str(self.repository),
            "base_branch": "",
            "branch_name": "task/example",
        }
        self.manager = WorkspaceManager(
            self.root / "workspaces", trusted_local_root=self.root
        )
        self.workspace = self.manager.prepare(str(uuid4()), self.item)
        self.sources = SourceStore(self.root / "supervisor-sources")

    def action(self, argv=None, **overrides):
        source = self.sources.register(
            "comment", "https://example.com/comment/1", b"Untrusted proposed command"
        )
        data = {
            "argv": argv or ["/usr/bin/true"],
            "mode": "exec",
            "source_kind": "comment",
            "source_reference": "https://example.com/comment/1",
            "source_digest": source,
            "workspace_id": self.workspace.workspace_id,
            "base_revision": self.workspace.base_revision,
            "content_digest": content_digest(self.workspace.checkout),
        }
        return ActionSpec.from_data({**data, **overrides})


class BrokerTests(unittest.TestCase):
    def setUp(self):
        self.run_id = str(uuid4())
        self.data = {
            "argv": ["/usr/bin/true"],
            "mode": "exec",
            "source_kind": "plan",
            "source_reference": "plan/1",
            "source_digest": "a" * 64,
            "workspace_id": self.run_id,
            "base_revision": "b" * 40,
            "content_digest": "c" * 64,
        }

    def test_repeated_consumption_never_grants_another_permit(self):
        class Client:
            def request(self, method, path, payload):
                return {"permit": False, "execution_id": payload["execution_id"]}

        broker = ApprovalBroker(Client(), self.run_id, "claim")
        with self.assertRaises(ApprovalRequired):
            broker.consume(str(uuid4()), ActionSpec.from_data(self.data))

    def test_request_rejects_foreign_workspace_before_network_call(self):
        class Client:
            def request(self, method, path, payload):
                raise AssertionError("Foreign workspace reached the API.")

        broker = ApprovalBroker(Client(), self.run_id, "claim")
        action = ActionSpec.from_data({**self.data, "workspace_id": str(uuid4())})
        with self.assertRaises(ApprovalRequired):
            broker.request(action, str(uuid4()))

    def test_consumption_rejects_foreign_workspace_before_network_call(self):
        class Client:
            def request(self, method, path, payload):
                raise AssertionError("Foreign workspace reached the API.")

        broker = ApprovalBroker(Client(), self.run_id, "claim")
        action = ActionSpec.from_data({**self.data, "workspace_id": str(uuid4())})
        with self.assertRaises(ApprovalRequired):
            broker.consume(str(uuid4()), action)


if __name__ == "__main__":
    unittest.main()
