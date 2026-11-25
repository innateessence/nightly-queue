import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

from .workspaces import (
    WorkspaceManager,
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


if __name__ == "__main__":
    unittest.main()
