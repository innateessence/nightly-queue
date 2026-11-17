import copy
import importlib.util
import unittest
from unittest.mock import patch

from vibearr_runner.backend_registry import ScopedCredentials
from vibearr_runner.native_runtime import runtime_diagnostics
from vibearr_runner.policy import default_execution_policy

if importlib.util.find_spec("vibearr_executor"):
    from vibearr_executor import SLOT_QUALIFICATION_VERSION as QUALIFICATION_VERSION
else:
    QUALIFICATION_VERSION = 0


class ExecutorFixture:
    def __init__(self):
        limits = {"cpu": 2, "memory_mb": 4096, "pids": 128, "workspace_mb": 2048}
        agent = {
            "profile_id": "native-default",
            "profile_digest": "a" * 64,
            "phase": "agent",
            "limits": limits,
            "network_hosts": ["api.openai.com"],
            "backend_secret_env": "OPENAI_API_KEY",
        }
        validation = {
            "profile_id": "project-default",
            "profile_digest": "b" * 64,
            "phase": "validation",
            "limits": limits,
            "network_hosts": [],
            "backend_secret_env": "",
        }
        self.document = {
            "schema_version": 1,
            "protocol_version": 2,
            "qualification_version": QUALIFICATION_VERSION,
            "runtime": "compose-slot-v2",
            "qualified": True,
            "accepting_work": True,
            "binding_digest": "c" * 64,
            "profiles": [agent, validation],
        }

    def capabilities(self):
        return copy.deepcopy(self.document)


class NativeRuntimeTests(unittest.TestCase):
    def policy(self):
        policy = default_execution_policy()
        policy["validation_profile"] = "project-default"
        return policy

    @unittest.skipUnless(
        importlib.util.find_spec("vibearr_executor"),
        "Install the native-runtime extra for executor capability tests",
    )
    def test_runtime_readiness_needs_no_production_delivery_or_backend_proof(self):
        result = runtime_diagnostics(self.policy(), ExecutorFixture())
        self.assertTrue(result["runtime_ready"])
        self.assertFalse(result["native_agent_ready"])
        self.assertEqual(result["binding_digest"], "c" * 64)

    def test_missing_optional_sdk_has_an_actionable_diagnostic(self):
        original_import = __import__

        def without_sdk(name, *args, **kwargs):
            if name.startswith("vibearr_executor"):
                raise ModuleNotFoundError("Executor SDK is intentionally absent")
            return original_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=without_sdk):
            result = runtime_diagnostics(self.policy(), ExecutorFixture())
        self.assertFalse(result["runtime_ready"])
        self.assertIn("vibearr-runner[native-runtime]", result["reason"])

    def test_stale_runtime_qualification_blocks_readiness(self):
        client = ExecutorFixture()
        client.document["qualified"] = False
        self.assertFalse(runtime_diagnostics(self.policy(), client)["runtime_ready"])

    def test_previous_qualification_version_blocks_readiness(self):
        client = ExecutorFixture()
        client.document["qualification_version"] = QUALIFICATION_VERSION - 1
        self.assertFalse(runtime_diagnostics(self.policy(), client)["runtime_ready"])

    def test_missing_selected_profile_blocks_readiness(self):
        client = ExecutorFixture()
        client.document["profiles"].pop()
        self.assertFalse(runtime_diagnostics(self.policy(), client)["runtime_ready"])

    def test_host_executor_proof_cannot_qualify_a_docker_worker(self):
        client = ExecutorFixture()
        client.document["runtime"] = "docker-oci-v1"
        client.document["protocol_version"] = 1
        self.assertFalse(runtime_diagnostics(self.policy(), client)["runtime_ready"])

    def test_disk_pressure_blocks_readiness(self):
        client = ExecutorFixture()
        client.document["accepting_work"] = False
        self.assertFalse(runtime_diagnostics(self.policy(), client)["runtime_ready"])

    def test_undersized_profile_blocks_readiness(self):
        client = ExecutorFixture()
        client.document["profiles"][0]["limits"]["memory_mb"] = 512
        self.assertFalse(runtime_diagnostics(self.policy(), client)["runtime_ready"])

    def test_credentialed_validation_profile_blocks_readiness(self):
        client = ExecutorFixture()
        client.document["profiles"][1]["backend_secret_env"] = "OPENAI_API_KEY"
        self.assertFalse(runtime_diagnostics(self.policy(), client)["runtime_ready"])

    def test_native_accounts_ignore_legacy_release_proof_but_can_preflight(self):
        class QueueFixture:
            def configuration(self):
                return {
                    "execution_kind": "native",
                    "release_verification": {
                        "qualified": True,
                        "accounts": [
                            {"backend": "codex", "credential_id": 1, "model": "model"}
                        ],
                    },
                }

            def request(self, method, path, data):
                return {"scope": data["scope"], "credential_id": data["credential_id"]}

        credentials = ScopedCredentials(QueueFixture())
        account = {"backend": "codex", "credential_id": 1, "model": "model"}
        self.assertIsNone(credentials.qualification(account))
        self.assertEqual(
            credentials(account), {"scope": "preflight", "credential_id": 1}
        )
