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
    def test_repeated_consumption_never_grants_another_permit(self):
        class Client:
            def request(self, method, path, payload):
                return {"permit": False, "execution_id": payload["execution_id"]}

        broker = ApprovalBroker(Client(), str(uuid4()), "claim")
        data = {
            "argv": ["/usr/bin/true"],
            "mode": "exec",
            "source_kind": "plan",
            "source_reference": "plan/1",
            "source_digest": "a" * 64,
            "workspace_id": str(uuid4()),
            "base_revision": "b" * 40,
            "content_digest": "c" * 64,
        }
        with self.assertRaises(ApprovalRequired):
            broker.consume(str(uuid4()), ActionSpec.from_data(data))


if __name__ == "__main__":
    unittest.main()
