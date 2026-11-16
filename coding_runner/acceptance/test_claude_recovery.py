"""Deterministic lifecycle replay with real Claude adapter parsing, never live evidence."""

import json
from unittest.mock import Mock, patch

from . import test_recovery as recovery
from ..backend_registry import BackendRegistry
from ..backend_transports import PROOF_PROTOCOL, TRANSPORTS
from ..backends import BackendRequest
from ..claude_backend import ClaudeBackend
from ..codex_backend import CodexBackend
from ..test_backends import response as codex_response
from ..test_claude_backend import grant, response


class ScenarioTransport:
    def __init__(self, provider="claude"):
        self.model = recovery.CandidateBackend()
        self.provider = provider
        self.payloads = []

    def exchange(self, payload, **kwargs):
        self.payloads.append(payload)
        request = payload["request"]
        if self.provider == "claude":
            instructions = request["system"]
            document = json.loads(request["messages"][0]["content"])
        else:
            instructions = request["instructions"]
            document = json.loads(request["input"])
        stage = document["stage"]
        selected = BackendRequest(
            {
                "backend": self.provider,
                "credential_id": 13 if self.provider == "claude" else 8,
            },
            request["model"],
            instructions,
            document,
        )
        event = getattr(self.model, stage)(selected)
        envelope = response if self.provider == "claude" else codex_response
        return envelope(event.kind, event.data)

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

    def mixed_stages(self, research_provider, implementation_provider):
        effective = self.state["run"]["run"]["configuration_snapshot"]["effective"]
        accounts = {
            "claude": {
                "backend": "claude",
                "credential_id": 13,
                "model": "claude-sonnet-5-5",
            },
            "codex": {
                "backend": "codex",
                "credential_id": 8,
                "model": "web-codex-model",
            },
        }
        entries = [
            {**account, "secret_version": 1, "transport": TRANSPORTS[provider]}
            for provider, account in accounts.items()
        ]

        def selected_grant(account):
            self.assertEqual(account, accounts[account["backend"]])
            value = grant(account)
            value["material"]["secret"] = "selected-secret-" + str(
                account["credential_id"]
            )
            return value

        credentials = Mock(side_effect=selected_grant)
        credentials.qualification.side_effect = lambda account: {
            **next(
                entry for entry in entries if entry["backend"] == account["backend"]
            ),
            "verification": {
                "qualified": True,
                "transport": PROOF_PROTOCOL,
                "accounts": entries,
            },
        }
        transports = {provider: ScenarioTransport(provider) for provider in accounts}
        backends = {
            "claude": ClaudeBackend(credentials, transport=transports["claude"]),
            "codex": CodexBackend(credentials, transport=transports["codex"]),
        }
        self.registry = BackendRegistry(
            {
                provider: lambda credentials, backend=backend: backend
                for provider, backend in backends.items()
            }
        )
        for stage, provider in (
            ("research", research_provider),
            ("implementation", implementation_provider),
        ):
            effective.update(
                {f"{stage}_{key}": value for key, value in accounts[provider].items()}
            )
        self.journal.save(self.state)
        self.until("approval")
        calls = {
            provider: len(transport.payloads)
            for provider, transport in transports.items()
        }
        self.restart()
        self.assertEqual(self.step(), "approval")
        self.assertEqual(
            {
                provider: len(transport.payloads)
                for provider, transport in transports.items()
            },
            calls,
        )
        self.api.decision = "approved"
        self.until("delivery_ready")
        for provider, transport in transports.items():
            self.assertEqual(len(transport.payloads), 2)
            self.assertEqual(
                {payload["token"] for payload in transport.payloads},
                {"selected-secret-" + str(accounts[provider]["credential_id"])},
            )
            self.assertEqual(
                {payload["request"]["model"] for payload in transport.payloads},
                {accounts[provider]["model"]},
            )

    def test_codex_research_and_claude_implementation_retain_separate_accounts(self):
        self.mixed_stages("codex", "claude")

    def test_claude_research_and_codex_implementation_retain_separate_accounts(self):
        self.mixed_stages("claude", "codex")
