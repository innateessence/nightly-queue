import hashlib
import shutil
import unittest

from . import test_native_lifecycle as lifecycle
from .test_native_claude import encoded, init_event, result_event
from vibearr_runner.native_claude import claude_profile
from vibearr_runner.native_sessions import NativeSession


class ClaudeExecutor(lifecycle.Executor):
    def __init__(self, profile):
        super().__init__(profile)
        self.profiles.remove(self.agent)
        self.agent = claude_profile("sha256:" + "a" * 64, "token")
        self.profiles.append(self.agent)

    def start(self, identity):
        result = super().start(identity)
        if self.manifests[identity]["profile_id"] != self.agent.profile_id:
            return result
        if self.logfiles[identity].startswith(b'{"type": "system"'):
            return result
        event = init_event()
        self.logfiles[identity] = encoded(event, result_event(event["session_id"]))
        self.states[identity]["result"]["log_digest"] = hashlib.sha256(
            self.logfiles[identity]
        ).hexdigest()
        return self.status(identity)


class ClaudeQueue(lifecycle.Queue):
    def request(self, method, path, payload=None):
        result = super().request(method, path, payload)
        if path.endswith("/secrets/") and payload.get("purpose") == "implementation":
            result["credential"]["provider"] = "claude"
            result["material"]["secret"] = "fixture-claude-key-0123456789"
        return result


@unittest.skipUnless(
    lifecycle.SDK_AVAILABLE and shutil.which("git"), "Requires runtime extra and Git."
)
class NativeClaudeLifecycleTests(unittest.TestCase):
    engine = lifecycle.NativeLifecycleTests.engine
    advance = lifecycle.NativeLifecycleTests.advance

    def setUp(self):
        lifecycle.NativeLifecycleTests.setUp(self)
        self.executor = ClaudeExecutor(self.profile)
        self.queue = ClaudeQueue(self.queue.source)
        self.host.client = self.queue
        snapshot = self.state["run"]["run"]["configuration_snapshot"]
        snapshot["execution_policy"].update(
            runtime_profile=self.executor.agent.profile_id,
            network_hosts=["api.anthropic.com"],
        )
        snapshot["effective"].update(
            implementation_backend="claude", implementation_model="claude-sonnet-5-5"
        )

    def test_fixture_restart_keeps_claude_session_and_validates_same_commit_with_all_checks(
        self,
    ):
        self.advance("import_candidate")
        started = list(self.executor.started)
        record = self.state["native"]["agent"]["session"]
        session = NativeSession.from_data(record)
        self.assertEqual(session.adapter, "claude")
        self.state = self.journal.load()
        self.advance("delivery")
        self.assertEqual(self.executor.started[:1], started)
        self.assertEqual(len(self.executor.started), 10)
        self.assertEqual(self.state["native"]["agent"]["session"], record)
        evidence = self.state["native"]["validation"]["evidence"]
        self.assertEqual(len(evidence["checks"]), 9)
        self.assertEqual(
            lifecycle.git(["rev-parse", "HEAD^{tree}"], self.workspace.checkout),
            evidence["tree_sha"],
        )
        self.assertEqual(self.queue.finishes, [])
