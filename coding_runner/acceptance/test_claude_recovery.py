"""Deterministic lifecycle replay with real Claude adapter parsing, never live evidence."""

import json
from unittest.mock import Mock, patch

from . import test_recovery as recovery
from ..backend_registry import BackendRegistry
from ..backend_transports import PROOF_PROTOCOL, TRANSPORTS
from ..backends import BackendRequest
from ..claude_backend import ClaudeBackend
from ..test_claude_backend import grant, response


class ScenarioTransport:
    def __init__(self):
        self.model = recovery.CandidateBackend()

    def exchange(self, payload, **kwargs):
        request = payload["request"]
        stage = (
            "research" if "Research allows" in request["system"] else "implementation"
        )
        document = json.loads(request["messages"][0]["content"])
        selected = BackendRequest(
            {"backend": "claude", "credential_id": 13},
            request["model"],
            request["system"],
            document,
        )
        event = getattr(self.model, stage)(selected)
        return response(event.kind, event.data)

    def interrupt(self):
        pass


class ScenarioClaude(ClaudeBackend):
    @property
    def research_calls(self):
        return self.transport.model.research_calls

    @property
    def implementation_calls(self):
        return self.transport.model.implementation_calls


class ClaudeLifecycleReplayTests(recovery.LifecycleRecoveryTests):
    def setUp(self):
        super().setUp()
        credentials = Mock(side_effect=grant)
        account = {
            "backend": "claude",
            "credential_id": 13,
            "model": "claude-sonnet-5-5",
        }
        entry = {**account, "secret_version": 1, "transport": TRANSPORTS["claude"]}
        credentials.qualification.return_value = {
            **entry,
            "verification": {
                "qualified": True,
                "transport": PROOF_PROTOCOL,
                "accounts": [entry],
            },
        }
        self.deployment = patch(
            "coding_runner.release.matches_deployment", return_value=True
        )
        self.deployment.start()
        self.addCleanup(self.deployment.stop)
        self.backend = ScenarioClaude(credentials, transport=ScenarioTransport())
        self.registry = BackendRegistry({"claude": lambda credentials: self.backend})
        effective = self.state["run"]["run"]["configuration_snapshot"]["effective"]
        for stage in ("research", "implementation"):
            effective.update(
                {
                    f"{stage}_backend": "claude",
                    f"{stage}_credential_id": 13,
                    f"{stage}_model": "claude-sonnet-5-5",
                }
            )
        self.journal.save(self.state)
