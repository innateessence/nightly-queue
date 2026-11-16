"""Run explicitly on the deployment host; unavailable namespaces fail this acceptance suite."""

import os
import socket
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from .approvals import ApprovalRequired
from .sandbox import ApprovedExecutor, probe_sandbox
from .test_workspaces import WorkspaceFixture


class OnePermit:
    def __init__(self, run_id, approved=True):
        self.run_id = run_id
        self.approved = approved
        self.consumed = False

    def consume(self, approval_id, action):
        if not self.approved or self.consumed:
            raise ApprovalRequired("No unused human approval.")
        self.consumed = True
        return str(uuid4())


class BoundaryChecks(WorkspaceFixture, unittest.TestCase):
    def setUp(self):
        self.create_workspace()
        probe_sandbox(self.workspace.checkout)

    def executor(self, broker=None, gate=None):
        return ApprovedExecutor(
            broker or OnePermit(self.workspace.workspace_id),
            self.sources,
            gate or (lambda: True),
        )

    def execute(self, script, **kwargs):
        python = (
            "/usr/local/bin/python3"
            if Path("/usr/local/bin/python3").exists()
            else "/usr/bin/python3"
        )
        action = self.action([python, "-c", script])
        return self.executor().execute(self.workspace, action, str(uuid4()), **kwargs)

    def test_cross_task_and_supervisor_files_are_inaccessible(self):
        other = self.manager.prepare(str(uuid4()), self.item)
        secret = self.root / "supervisor-secret"
        secret.write_text("secret")
        paths = [
            str(secret),
            str(other.checkout / "README.md"),
            str(self.workspace.directory / "workspace.json"),
            "/workspace/.git/config",
            "/etc/passwd",
            "/home/jack",
        ]
        script = (
            "import os; paths="
            + repr(paths)
            + "; assert all(not os.path.exists(path) for path in paths)"
        )
        self.assertEqual(self.execute(script).exit_code, 0)

    def test_environment_and_inherited_file_descriptors_do_not_expose_secrets(self):
        secret = self.root / "secret"
        secret.write_text("supervisor-secret-value")
        descriptor = os.open(secret, os.O_RDONLY)
        self.addCleanup(os.close, descriptor)
        os.set_inheritable(descriptor, True)
        script = "import os; assert 'supervisor-secret-value' not in str(dict(os.environ)); assert all(int(fd) < 4 for fd in os.listdir('/proc/self/fd')); assert os.environ['HOME']=='/home/agent'"
        with patch.dict(
            os.environ,
            {
                "NQ_RUNNER_TOKEN": "supervisor-secret-value",
                "GH_TOKEN": "supervisor-secret-value",
            },
        ):
            self.assertEqual(self.execute(script).exit_code, 0)

    def test_parent_process_and_network_are_inaccessible(self):
        namespace = os.readlink("/proc/self/ns/pid")
        listener = socket.socket()
        self.addCleanup(listener.close)
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        script = "import os,socket; assert os.readlink('/proc/self/ns/pid') != " + repr(
            namespace
        )
        script += (
            "; assert set(p for p in os.listdir('/proc') if p.isdigit()) <= {'1','2'}"
        )
        script += (
            "; s=socket.socket(); assert s.connect_ex(('127.0.0.1', "
            + str(port)
            + ")) != 0"
        )
        self.assertEqual(self.execute(script).exit_code, 0)

    def test_agent_cannot_write_git_config(self):
        script = "from pathlib import Path; Path('/workspace/.git/config').write_text('malicious')"
        original = (self.workspace.checkout / ".git/config").read_bytes()
        result = self.execute(script)
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(
            (self.workspace.checkout / ".git/config").read_bytes(), original
        )

    def test_symlink_to_host_supervisor_secret_cannot_be_read(self):
        secret = self.root / "private"
        secret.write_text("supervisor")
        (self.workspace.checkout / "escape").symlink_to(secret)
        self.assertNotEqual(
            self.execute("open('/workspace/escape').read()").exit_code, 0
        )

    def test_malicious_argument_is_literal_until_explicit_shell_approval(self):
        value = "$(touch /workspace/injected); `touch /workspace/injected`"
        action = self.action(["/usr/bin/printf", "%s", value])
        result = self.executor().execute(self.workspace, action, str(uuid4()))
        self.assertEqual(result.output.decode(), value)
        self.assertFalse((self.workspace.checkout / "injected").exists())

    def test_injected_command_and_denied_action_do_not_execute(self):
        action = self.action(["/usr/bin/touch", "/workspace/injected"])
        with self.assertRaises(ApprovalRequired):
            self.executor(
                OnePermit(self.workspace.workspace_id, approved=False)
            ).execute(self.workspace, action, str(uuid4()))
        self.assertFalse((self.workspace.checkout / "injected").exists())

    def test_workspace_change_invalidates_even_approved_action(self):
        action = self.action(["/usr/bin/touch", "/workspace/injected"])
        (self.workspace.checkout / "README.md").write_text("changed")
        with self.assertRaises(ApprovalRequired):
            self.executor().execute(self.workspace, action, str(uuid4()))
        self.assertFalse((self.workspace.checkout / "injected").exists())

    def test_source_change_invalidates_even_approved_action(self):
        action = self.action(["/usr/bin/touch", "/workspace/injected"])
        self.sources.register(
            action.source_kind, action.source_reference, b"changed source"
        )
        with self.assertRaises(ApprovalRequired):
            self.executor().execute(self.workspace, action, str(uuid4()))
        self.assertFalse((self.workspace.checkout / "injected").exists())

    def test_timeout_kills_forked_children(self):
        script = "import os,time; child=os.fork(); time.sleep(2) if child == 0 else None; open('/workspace/escaped','w').write('bad') if child == 0 else time.sleep(30)"
        result = self.execute(script, timeout=0.2)
        self.assertTrue(result.interrupted)
        subprocess.run(["/usr/bin/sleep", "2.2"], check=True)
        self.assertFalse((self.workspace.checkout / "escaped").exists())

    def test_process_exit_during_kill_still_collects_result(self):
        action = self.action(["/usr/bin/sleep", "30"])
        killpg = os.killpg

        def disappearing_group(pid, sig):
            killpg(pid, sig)
            raise ProcessLookupError

        executor = ApprovedExecutor(
            OnePermit(self.workspace.workspace_id),
            self.sources,
            lambda: True,
            periodic_gate=lambda: False,
        )
        with patch("coding_runner.sandbox.os.killpg", side_effect=disappearing_group):
            result = executor.execute(self.workspace, action, str(uuid4()))
        self.assertTrue(result.interrupted)
        self.assertLess(result.exit_code, 0)

    def test_child_cannot_survive_successful_parent_exit(self):
        script = "import os,time; child=os.fork(); time.sleep(1) if child == 0 else None; open('/workspace/escaped','w').write('bad') if child == 0 else os._exit(0)"
        self.assertEqual(self.execute(script).exit_code, 0)
        subprocess.run(["/usr/bin/sleep", "1.2"], check=True)
        self.assertFalse((self.workspace.checkout / "escaped").exists())

    def test_closed_gate_never_consumes_approval(self):
        action = self.action(["/usr/bin/touch", "/workspace/injected"])
        broker = OnePermit(self.workspace.workspace_id)
        with self.assertRaises(ApprovalRequired):
            self.executor(broker, lambda: False).execute(
                self.workspace, action, str(uuid4())
            )
        self.assertFalse(broker.consumed)

    def test_research_checkout_is_read_only_even_with_human_approval(self):
        action = self.action(["/usr/bin/touch", "/workspace/research-write"])
        executor = ApprovedExecutor(
            OnePermit(self.workspace.workspace_id),
            self.sources,
            lambda: True,
            read_only=True,
        )
        result = executor.execute(self.workspace, action, str(uuid4()))
        self.assertNotEqual(result.exit_code, 0)
        self.assertFalse((self.workspace.checkout / "research-write").exists())


if __name__ == "__main__":
    unittest.main()
