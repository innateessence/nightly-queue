from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from coding_runner.test_automatic_merge import MergeHost, profile, report
from vibearr_runner.delivery import DeliveryUnknown
from vibearr_runner.recovery import ReconciliationRequired
from vibearr_runner.review_publication import ReviewPublication
from vibearr_runner.workspaces import Workspace, git


class LocalTransport:
    def __init__(self, remote):
        self.remote_path = remote
        self.pushes = 0
        self.lose_push = False

    def command(self, arguments, repository, directory, cwd=None):
        arguments = list(arguments)
        arguments[-2] = str(self.remote_path)
        return git(arguments, cwd, local=True)

    def observed_remote(self, workspace):
        output = git(
            [
                "ls-remote",
                "--refs",
                str(self.remote_path),
                "refs/heads/" + workspace.branch_name,
            ],
            local=True,
        )
        return output.split()[0] if output else ""

    def push_once(self, workspace, commit, *, on_launch):
        on_launch()
        self.pushes += 1
        git(
            [
                "push",
                "--",
                str(self.remote_path),
                commit + ":refs/heads/" + workspace.branch_name,
            ],
            workspace.checkout,
            local=True,
        )
        if self.lose_push:
            self.lose_push = False
            raise DeliveryUnknown(
                "Actual Git receive completed; response deliberately lost"
            )


class ReviewControlGitTests(unittest.TestCase):
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

    def test_control_commit_and_lost_push_preserve_source_head_and_target(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            remote = root / "remote.git"
            source = root / "source"
            git(["init", "--bare", str(remote)])
            git(["init", "--initial-branch=main", str(source)])
            (source / "app.py").write_text("value = 1\n")
            base = self.commit(source, "Accepted app base")
            git(
                [
                    "push",
                    str(remote),
                    "HEAD:refs/heads/main",
                    "HEAD:refs/heads/vibearr/reviews",
                ],
                source,
                local=True,
            )
            git(["checkout", "-b", "task/1"], source)
            (source / "app.py").write_text("value = 2\n")
            candidate = self.commit(source, "App task")
            host = MergeHost()
            identity = {
                **host.native["candidate"]["workspace"],
                "base_revision": base,
                "commit_sha": candidate,
                "tree_sha": git(["rev-parse", "HEAD^{tree}"], source),
            }
            host.native["candidate"]["workspace"] = identity
            host.native["delivery"]["workspace"] = identity
            host.native["delivery"]["pull_request"]["head_sha"] = candidate
            host.state = {"native": host.native}
            host.journal = SimpleNamespace(save=lambda state: None)
            host.workspace = lambda: Workspace(
                host.run_id,
                "https://github.com/owner/repo.git",
                "main",
                "main",
                base,
                "task/1",
                root,
            )
            host.secret = lambda *args, **kwargs: {"material": {"secret": "fixture"}}
            host.read = lambda key, operation: operation()
            registered = {}
            host.intent = lambda payload, key: registered.setdefault(key, str(uuid4()))
            host.artifact = lambda *args: str(uuid4())
            reviews = {
                "roles": {
                    role: {
                        "fact_id": str(uuid4()),
                        "result": {
                            "report": {
                                **report(role, host.run_id),
                                "head_sha": candidate,
                                "base_sha": base,
                                "tree_sha": identity["tree_sha"],
                            },
                            "receipt": {"protected": "fixture"},
                        },
                    }
                    for role in ("independent", "root")
                }
            }
            transport = LocalTransport(remote)
            github = SimpleNamespace(base_sha=lambda workspace: base)
            publication = ReviewPublication(host, reviews)
            publication.prepare(profile(), github, transport)
            binding = publication.data["binding"]
            checkout = publication.local().checkout
            self.assertEqual(binding["control_base"], "vibearr/reviews")
            self.assertEqual(binding["base_revision"], base)
            self.assertEqual(len(binding["files"]), 4)
            self.assertTrue(
                all(value["path"].endswith(".json") for value in binding["files"])
            )
            self.assertEqual(git(["rev-parse", "HEAD"], source), candidate)
            self.assertEqual(git(["status", "--porcelain"], source), "")
            self.assertEqual(
                git(["diff", "--name-only", base, "HEAD"], checkout).splitlines(),
                sorted(value["path"] for value in binding["files"]),
            )
            transport.lose_push = True
            with patch.object(
                ReviewPublication, "clients", return_value=(github, transport)
            ):
                with self.assertRaises(ReconciliationRequired):
                    publication.step(profile())
                self.assertEqual(publication.step(profile()), "continue")
            self.assertEqual(transport.pushes, 1)
            self.assertIn("push", publication.data["facts"])
            self.assertEqual(
                transport.observed_remote(publication.local()), binding["commit_sha"]
            )
            self.assertEqual(git(["rev-parse", "HEAD"], source), candidate)


if __name__ == "__main__":
    unittest.main()
