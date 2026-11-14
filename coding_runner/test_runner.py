import subprocess
import shutil
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from .config import Config
from .http import APIError
from .process import clean_environment
from .repository import Repository
from .runner import Runner


class Queue:
    def __init__(self, item):
        self.item = item
        self.finishes = []
        self.claims = []
        self.fail_finish = False
        self.logs = []

    def log(self, item, run_id, sequence, text):
        self.logs.append(text)

    def claim(self, run_id):
        self.claims.append(run_id)
        return self.item

    def finish(self, item, outcome):
        if self.fail_finish:
            raise APIError("Finish response lost.")
        self.finishes.append(outcome)
        self.item = dict(item, **outcome)

    def lookup(self, run_id):
        return self.item


class GitHub:
    def __init__(self):
        self.calls = []

    def pull_request(self, *args):
        self.calls.append(args)
        return "https://github.com/example/repo/pull/1"


class Agent:
    def __init__(self):
        self.calls = 0

    def run(self, commands, workspace, env, item):
        self.calls += 1
        if any(name in env for name in ("GH_TOKEN", "NQ_API_TOKEN", "OPENAI_API_KEY")):
            raise AssertionError(
                "Runner credentials leaked into the agent environment."
            )
        (workspace / "README.md").write_text("Implemented\n")


@unittest.skipUnless(
    shutil.which("git"), "Git integration tests run in the coding-runner image."
)
class RunnerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.seed = self.root / "seed"
        self.seed.mkdir()
        self.env = clean_environment(self.root)
        self.git(self.seed, "init", "-b", "main")
        (self.seed / "README.md").write_text("Original\n")
        self.git(self.seed, "add", ".")
        self.git(self.seed, "commit", "-m", "Initial")
        self.remote = self.root / "remote.git"
        self.git(self.root, "clone", "--bare", str(self.seed), str(self.remote))
        validation = [
            [
                sys.executable,
                "-c",
                "from pathlib import Path; assert Path('README.md').read_text() == 'Implemented\\n'",
            ]
        ]
        self.config = Config(
            "http://queue",
            "queue-secret",
            "github-secret",
            "openai-secret",
            self.root / "data",
            {"example/repo": {"validation": validation}},
        )
        self.item = {
            "id": 7,
            "title": "Update README",
            "instructions": "Implement task",
            "repository_url": "https://github.com/example/repo.git",
            "base_branch": "",
            "branch_name": "nq/7",
            "claim_token": "claim-secret",
            "status": "running",
        }
        self.queue = Queue(self.item)
        self.github = GitHub()
        self.agent = Agent()
        self.runner = Runner(self.config, self.queue, self.github, self.agent)
        factory = patch("coding_runner.runner.Repository", side_effect=self.repository)
        factory.start()
        self.addCleanup(factory.stop)

    def repository(self, *args):
        repository = Repository(*args)
        repository.url = str(self.remote)
        return repository

    def git(self, cwd, *args):
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            env=self.env,
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip()

    def test_task_runs_validation_pushes_branch_and_reports_real_pr(self):
        self.assertTrue(self.runner.once())
        self.assertEqual(
            self.queue.finishes,
            [
                {
                    "status": "succeeded",
                    "result_url": "https://github.com/example/repo/pull/1",
                }
            ],
        )
        self.assertEqual(self.git(self.remote, "show", "nq/7:README.md"), "Implemented")
        self.assertEqual(self.github.calls[0][2], "main")
        self.assertIsNone(self.runner.journal.read())
        self.assertIn("[validation]", "".join(self.queue.logs))
        self.assertIn("PR delivered", "".join(self.queue.logs))

    def test_validation_failure_does_not_push_or_create_pr(self):
        self.config.policies["example/repo"]["validation"] = [
            [sys.executable, "-c", "raise SystemExit(1)"]
        ]
        self.runner.once()
        self.assertEqual(self.queue.finishes[0]["status"], "failed")
        self.assertEqual(self.github.calls, [])
        self.assertEqual(self.git(self.remote, "branch", "--list", "nq/7"), "")

    def test_unapproved_repository_is_failed_without_invoking_agent(self):
        self.config.policies.clear()
        self.runner.once()
        self.assertEqual(self.agent.calls, 0)
        self.assertEqual(self.queue.finishes[0]["status"], "failed")

    def test_empty_queue_does_not_invoke_agent(self):
        self.queue.item = None
        self.assertFalse(self.runner.once())
        self.assertEqual(self.agent.calls, 0)

    def test_closed_execution_window_does_not_claim(self):
        with patch.object(Config, "window_open", return_value=False):
            self.assertFalse(self.runner.once())
        self.assertEqual(self.queue.claims, [])

    def test_finish_failure_preserves_delivery_and_recovers_without_rerunning_agent(
        self,
    ):
        self.queue.fail_finish = True
        with self.assertRaises(APIError):
            self.runner.once()
        state = self.runner.journal.read()
        self.assertEqual(state["phase"], "finishing")
        self.assertEqual(
            state["outcome"]["result_url"], "https://github.com/example/repo/pull/1"
        )
        self.queue.fail_finish = False
        self.runner.recover()
        self.assertEqual(self.agent.calls, 1)
        self.assertEqual(len(self.github.calls), 1)
        self.assertIsNone(self.runner.journal.read())

    def test_lost_finish_response_reconciles_already_completed_work(self):
        self.queue.fail_finish = True
        with self.assertRaises(APIError):
            self.runner.once()
        self.queue.item = dict(
            self.item,
            status="succeeded",
            result_url="https://github.com/example/repo/pull/1",
        )
        self.runner.recover()
        self.assertEqual(self.queue.finishes, [])
        self.assertIsNone(self.runner.journal.read())

    def test_ambiguous_claim_is_not_retried(self):
        with patch.object(self.queue, "claim", side_effect=APIError("Response lost")):
            with self.assertRaises(APIError):
                self.runner.once()
        self.assertEqual(self.runner.journal.read()["phase"], "claiming")
        with self.assertRaises(RuntimeError):
            self.runner.recover()
        with self.assertRaises(RuntimeError):
            self.runner.once()

    def test_stale_token_cannot_report_old_outcome_after_requeue(self):
        self.queue.fail_finish = True
        with self.assertRaises(APIError):
            self.runner.once()
        self.queue.item = dict(self.item, claim_token="replacement")
        with self.assertRaises(RuntimeError):
            self.runner.recover()
        self.assertEqual(self.queue.finishes, [])

    def test_ambiguous_pr_creation_stops_further_claims(self):
        with patch.object(
            self.github, "pull_request", side_effect=APIError("Response lost")
        ):
            with self.assertRaises(APIError):
                self.runner.once()
        self.assertEqual(self.runner.journal.read()["phase"], "delivery")
        self.assertEqual(self.queue.finishes, [])
        with self.assertRaises(RuntimeError):
            self.runner.once()

    def test_active_run_cannot_be_discarded(self):
        self.runner.journal.write({"run_id": "test-run", "phase": "agent"})
        with self.assertRaises(RuntimeError):
            self.runner.recover(discard=True)

    def test_requeued_run_can_be_discarded(self):
        self.runner.journal.write({"run_id": "test-run", "phase": "agent"})
        self.queue.item = None
        self.runner.recover(discard=True)
        self.assertIsNone(self.runner.journal.read())

    def test_no_changes_is_not_reported_as_success(self):
        with patch.object(self.agent, "run"):
            self.config.policies["example/repo"]["validation"] = [
                [sys.executable, "-c", "pass"]
            ]
            self.runner.once()
        self.assertEqual(self.queue.finishes[0]["status"], "failed")
        self.assertEqual(self.github.calls, [])

    def test_retry_preserves_existing_remote_branch_work(self):
        self.git(self.seed, "checkout", "-b", "nq/7")
        (self.seed / "previous.txt").write_text("Keep this")
        self.git(self.seed, "add", ".")
        self.git(self.seed, "commit", "-m", "Prior attempt")
        self.git(self.seed, "push", str(self.remote), "nq/7")
        self.runner.once()
        self.assertEqual(
            self.git(self.remote, "show", "nq/7:previous.txt"), "Keep this"
        )

    def test_second_runner_cannot_share_a_locked_volume(self):
        other = Runner(replace(self.config), self.queue, self.github, self.agent)
        with self.runner.journal.locked():
            with self.assertRaises(RuntimeError):
                with other.journal.locked():
                    self.fail("Second runner acquired the lock")

    def test_changed_git_configuration_prevents_delivery(self):
        def tamper(commands, workspace, env, item):
            self.agent.calls += 1
            (workspace / "README.md").write_text("Implemented\n")
            with (workspace / ".git" / "config").open("a") as config:
                config.write("\n[credential]\n\thelper = untrusted\n")

        with patch.object(self.agent, "run", side_effect=tamper):
            self.runner.once()
        self.assertEqual(self.queue.finishes[0]["status"], "failed")
        self.assertEqual(self.github.calls, [])
