import unittest
from unittest.mock import Mock

from .policy import ExecutionPolicy, PolicyError, default_execution_policy
from .lifecycle import Lifecycle
from .recovery import ReconciliationRequired


class ExecutionPolicyTests(unittest.TestCase):
    def test_policy_round_trip_preserves_digest(self):
        original = ExecutionPolicy()
        recovered = ExecutionPolicy.from_data(original.to_data())
        self.assertEqual(recovered, original)
        self.assertEqual(recovered.digest, original.digest)

    def test_unknown_policy_version_is_rejected(self):
        value = default_execution_policy()
        value["schema_version"] = 2
        with self.assertRaises(PolicyError):
            ExecutionPolicy.from_data(value)

    def test_host_mount_capability_is_rejected(self):
        value = default_execution_policy()
        value["mounts"] = ["/"]
        with self.assertRaises(PolicyError):
            ExecutionPolicy.from_data(value)

    def test_resource_boolean_is_not_a_numeric_limit(self):
        value = default_execution_policy()
        value["cpu_limit"] = True
        with self.assertRaises(PolicyError):
            ExecutionPolicy.from_data(value)

    def test_network_policy_does_not_accept_url_or_wildcard(self):
        for host in [
            "https://example.com",
            "*.example.com",
            "127.0.0.1",
            "foo.internal",
        ]:
            with self.subTest(host=host), self.assertRaises(PolicyError):
                ExecutionPolicy.from_data(
                    {**default_execution_policy(), "network_hosts": [host]}
                )

    def test_legacy_execution_kind_is_rejected(self):
        value = default_execution_policy()
        value["execution_kind"] = "structured_proposals"
        with self.assertRaises(PolicyError):
            ExecutionPolicy.from_data(value)

    def test_legacy_release_proof_cannot_reactivate_proposal_execution(self):
        lifecycle = Lifecycle(Mock(), Mock(), Mock(), workspace_manager=Mock())
        self.assertFalse(
            lifecycle.ready(
                {"execution_ready": True, "release_verification": {"qualified": True}}
            )
        )

    def test_native_claim_cannot_fall_through_to_legacy_dispatch(self):
        checkpoint = Mock(return_value=True)
        lifecycle = Lifecycle(Mock(), Mock(), checkpoint, workspace_manager=Mock())
        state = {
            "run": {"run": {"configuration_snapshot": {"execution_kind": "native"}}}
        }
        with self.assertRaisesRegex(ReconciliationRequired, "Native task dispatch"):
            lifecycle.step(state)
        checkpoint.assert_not_called()
