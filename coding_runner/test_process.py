import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

from .codex import Codex
from .process import Commands, TaskError, clean_environment


class ProcessTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.workspace = self.root / "repository"
        self.workspace.mkdir()
        self.env = clean_environment(self.home)
        self.commands = Commands(self.root / "commands.log", 10, ["private-api-key"])

    def test_arguments_are_passed_without_shell_expansion(self):
        argument = "$(touch injected); echo untrusted"
        output = self.commands.run(
            [sys.executable, "-c", "import sys; print(sys.argv[1])", argument],
            self.workspace,
            self.env,
        )
        self.assertEqual(output, argument)
        self.assertFalse((self.workspace / "injected").exists())

    def test_known_secrets_are_redacted_from_logs(self):
        self.commands.run(
            [sys.executable, "-c", "print('private-api-key')"], self.workspace, self.env
        )
        self.assertNotIn("private-api-key", self.commands.log_path.read_text())

    def test_timeout_terminates_descendants_even_after_parent_exits(self):
        self.commands.deadline = time.monotonic() + 0.2
        script = (
            "import os,time; child=os.fork(); time.sleep(30) if child == 0 else None"
        )
        start = time.monotonic()
        with self.assertRaisesRegex(TaskError, "timeout"):
            self.commands.run([sys.executable, "-c", script], self.workspace, self.env)
        self.assertLess(time.monotonic() - start, 8)

    def test_failed_command_stops_execution(self):
        with self.assertRaises(TaskError):
            self.commands.run(
                [sys.executable, "-c", "raise SystemExit(2)"], self.workspace, self.env
            )

    def fake_codex(self, status):
        binary = self.root / "codex"
        script = f"#!{sys.executable}\n"
        script += "import json,os,sys\nfrom pathlib import Path\n"
        script += (
            "assert 'GH_TOKEN' not in os.environ and 'NQ_API_TOKEN' not in os.environ\n"
        )
        script += "assert os.environ['CODEX_API_KEY'] == 'private-api-key'\n"
        script += "assert '--sandbox' in sys.argv and 'workspace-write' in sys.argv\n"
        script += "assert not any('dangerously' in arg for arg in sys.argv)\n"
        script += "Path('prompt.txt').write_text(sys.stdin.read())\n"
        script += "result=Path(sys.argv[sys.argv.index('--output-last-message')+1])\n"
        script += f"result.write_text(json.dumps({{'status': {status!r}, 'summary': 'Finished'}}))\n"
        binary.write_text(script)
        binary.chmod(0o755)
        self.env["PATH"] = str(self.root) + os.pathsep + self.env["PATH"]

    def test_codex_receives_task_as_stdin_and_reports_completed_result(self):
        self.fake_codex("completed")
        title = "$(touch injected)"
        Codex("private-api-key").run(
            self.commands, self.workspace, self.env, {"title": title}
        )
        prompt = (self.workspace / "prompt.txt").read_text()
        self.assertIn(title, prompt)
        self.assertNotIn("private-api-key", prompt)
        self.assertFalse((self.workspace / "injected").exists())

    def test_codex_blocked_result_prevents_delivery(self):
        self.fake_codex("blocked")
        with self.assertRaisesRegex(TaskError, "approval"):
            Codex("private-api-key").run(
                self.commands, self.workspace, self.env, {"title": "Task"}
            )

    def test_clean_environment_excludes_orchestrator_credentials(self):
        self.assertNotIn("GH_TOKEN", self.env)
        self.assertNotIn("NQ_API_TOKEN", self.env)
        self.assertNotIn("OPENAI_API_KEY", self.env)
