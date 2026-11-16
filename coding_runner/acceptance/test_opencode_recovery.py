"""Deterministic real-adapter lifecycle replay; these fixtures cannot qualify a release."""

import json
from unittest.mock import Mock, patch

from . import test_recovery as recovery
from .test_claude_recovery import ScenarioClaude
from ..backend_registry import BackendRegistry
from ..backend_transports import PROOF_PROTOCOL, TRANSPORTS
from ..backends import BackendRequest
from ..opencode_backend import OpenCodeBackend
from ..test_opencode_backend import grant, response
from ..test_claude_backend import grant as claude_grant, response as claude_response


class ScenarioTransport:
    def __init__(self, model=None):
        self.model = model or recovery.CandidateBackend()

    def exchange(self, payload, **kwargs):
        request = payload["request"]
        system = request.get("system") or request["messages"][0]["content"]
        stage = "research" if "Research allows" in system else "implementation"
        document = json.loads(request["messages"][-1]["content"])
        selected = BackendRequest({}, request["model"], system, document)
        event = getattr(self.model, stage)(selected)
        value = (
            claude_response(event.kind, event.data)
            if "system" in request
            else response(event.kind, event.data)
        )
        value["response"]["model"] = request["model"]
        return value

    def interrupt(self):
        pass


class ScenarioOpenCode(OpenCodeBackend):
    @property
    def research_calls(self):
        return self.transport.model.research_calls

    @property
    def implementation_calls(self):
        return self.transport.model.implementation_calls


class OpenCodeLifecycleReplayTests(recovery.LifecycleRecoveryTests):
    def setUp(self):
        super().setUp()
        self.credentials = Mock(side_effect=grant)
        self.account = {
            "backend": "opencode",
            "credential_id": 14,
            "model": "qwen3-coder:30b",
        }
        self.entry = {
            **self.account,
            "secret_version": 1,
            "transport": TRANSPORTS["opencode"],
        }
        self.entries = [self.entry]
        self.credentials.qualification.side_effect = self.qualification
        deployment = patch(
            "coding_runner.release.matches_deployment", return_value=True
        )
        deployment.start()
        self.addCleanup(deployment.stop)
        self.backend = ScenarioOpenCode(self.credentials, transport=ScenarioTransport())
        self.registry = BackendRegistry({"opencode": lambda credentials: self.backend})
        effective = self.state["run"]["run"]["configuration_snapshot"]["effective"]
        for stage in ("research", "implementation"):
            for key, value in self.account.items():
                effective[f"{stage}_{key}"] = value
        self.journal.save(self.state)

    def qualification(self, account):
        entry = next(
            row
            for row in self.entries
            if all(
                row.get(key) == account.get(key)
                for key in ("backend", "credential_id", "model")
            )
        )
        return {
            **entry,
            "verification": {
                "qualified": True,
                "transport": PROOF_PROTOCOL,
                "accounts": self.entries,
            },
        }


class MixedBackendLifecycleReplayTests(OpenCodeLifecycleReplayTests):
    def setUp(self):
        super().setUp()
        account = {
            "backend": "claude",
            "credential_id": 13,
            "model": "claude-sonnet-5-5",
        }
        self.entries.append(
            {**account, "secret_version": 1, "transport": TRANSPORTS["claude"]}
        )
        credentials = Mock(side_effect=claude_grant)
        credentials.qualification.side_effect = self.qualification
        claude = ScenarioClaude(
            credentials, transport=ScenarioTransport(self.backend.transport.model)
        )
        self.registry = BackendRegistry(
            {
                "opencode": lambda credentials: self.backend,
                "claude": lambda credentials: claude,
            }
        )
        effective = self.state["run"]["run"]["configuration_snapshot"]["effective"]
        for key, value in account.items():
            effective[f"research_{key}"] = value
        self.journal.save(self.state)
