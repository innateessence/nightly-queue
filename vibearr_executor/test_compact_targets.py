import io
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from .client import ExecutorRejected
from .compact_protocol import qualification_targets
from .compact_worker import CompactHandler, main
from .contracts import Rejected
from . import test_compact_protocol as protocol_tests


TARGETS = {"app": "10.41.0.2", "postgres": "172.19.0.2", "runner": "192.168.40.2"}


class QualificationTargetTests(unittest.TestCase):
    def test_private_service_targets_are_copied_without_permissions(self):
        observed = qualification_targets(TARGETS)
        self.assertEqual(observed, TARGETS)
        self.assertIsNot(observed, TARGETS)

    def test_missing_service_is_rejected(self):
        with self.assertRaises(Rejected):
            qualification_targets(
                {key: value for key, value in TARGETS.items() if key != "runner"}
            )

    def test_unknown_service_is_rejected(self):
        with self.assertRaises(Rejected):
            qualification_targets(
                {
                    "worker": TARGETS["app"],
                    "postgres": TARGETS["postgres"],
                    "runner": TARGETS["runner"],
                }
            )

    def test_extra_service_is_rejected(self):
        with self.assertRaises(Rejected):
            qualification_targets({**TARGETS, "external": "10.41.0.9"})

    def test_nonprivate_and_nonnumeric_addresses_are_rejected(self):
        addresses = [
            "1.1.1.1",
            "127.0.0.1",
            "169.254.169.254",
            "0.0.0.0",
            "224.0.0.1",
            "192.0.2.1",
            "::1",
            "app",
            "010.0.0.1",
            "10.0.0.2:8000",
            "10.0.0.2" * 100,
            123,
            True,
            None,
        ]
        for address in addresses:
            with self.subTest(address=address), self.assertRaises(Rejected):
                qualification_targets({**TARGETS, "app": address})

    def test_duplicate_service_addresses_are_rejected(self):
        with self.assertRaises(Rejected):
            qualification_targets({**TARGETS, "app": TARGETS["runner"]})


class QualificationTargetHTTPTests(unittest.TestCase):
    setUp = protocol_tests.CompactWriteProtocolTests.setUp
    endpoint = protocol_tests.CompactWriteProtocolTests.endpoint

    def test_versioned_target_map_reaches_current_compact_qualification(self):
        qualify = Mock(return_value={"received": True})
        module = SimpleNamespace(qualify=qualify)
        with (
            patch.dict(
                "sys.modules", {"vibearr_executor.compact_qualification": module}
            ),
            self.endpoint(self.compact, CompactHandler) as client,
        ):
            observed = client.request("/v4/qualify", {"targets": TARGETS})
        self.assertEqual(observed, {"received": True})
        qualify.assert_called_once_with(self.compact, targets=TARGETS)
        self.assertFalse(self.compact.qualifying)

    def test_missing_targets_are_rejected_before_qualification(self):
        with self.endpoint(self.compact, CompactHandler) as client:
            with self.assertRaises(ExecutorRejected):
                client.request("/v4/qualify", {})

    def test_extra_control_fields_cannot_change_qualification(self):
        with self.endpoint(self.compact, CompactHandler) as client:
            with self.assertRaises(ExecutorRejected):
                client.request(
                    "/v4/qualify", {"targets": TARGETS, "network_hosts": ["external"]}
                )

    def test_invalid_service_targets_cannot_reach_qualification(self):
        with self.endpoint(self.compact, CompactHandler) as client:
            with self.assertRaises(ExecutorRejected):
                client.request(
                    "/v4/qualify", {"targets": {**TARGETS, "runner": "127.0.0.1"}}
                )

    def test_legacy_runtime_cannot_accept_versioned_qualification_targets(self):
        with self.endpoint(self.legacy, CompactHandler) as client:
            with self.assertRaises(ExecutorRejected):
                client.request("/v4/qualify", {"targets": TARGETS})

    def test_unversioned_targets_cannot_enter_compact_qualification(self):
        with self.endpoint(self.compact, CompactHandler) as client:
            client.expected_runtime = ""
            with self.assertRaises(ExecutorRejected):
                client.request("/v4/qualify", {"targets": TARGETS})


class QualificationTargetCLITests(unittest.TestCase):
    def invoke(self, action, raw_targets):
        client = Mock()
        client.request.return_value = {"received": True}
        with (
            patch("sys.argv", ["compact-worker", action]),
            patch("vibearr_executor.compact_worker.os.umask"),
            patch(
                "vibearr_executor.compact_worker.read_control_token",
                return_value="x" * 48,
            ),
            patch("vibearr_executor.compact_worker.Client", return_value=client),
            patch.dict(
                "os.environ",
                {
                    "VIBEARR_QUALIFICATION_TARGETS": raw_targets,
                    "VIBEARR_EXECUTOR_TOKEN_FILE": "/tmp/qualification-test-token",
                },
            ),
            patch("sys.stdout", new_callable=io.StringIO),
        ):
            main()
        return client

    def test_qualify_cli_sends_the_inspected_target_map(self):
        client = self.invoke("qualify", json.dumps(TARGETS))
        client.request.assert_called_once_with("/v4/qualify", {"targets": TARGETS})

    def test_qualify_cli_rejects_missing_targets(self):
        with self.assertRaises(Rejected):
            self.invoke("qualify", "")

    def test_qualify_cli_rejects_oversized_json(self):
        with self.assertRaises(Rejected):
            self.invoke("qualify", " " * 513 + json.dumps(TARGETS))

    def test_qualify_cli_rejects_nonobject_json(self):
        with self.assertRaises(Rejected):
            self.invoke("qualify", "[]")

    def test_other_management_actions_ignore_target_environment(self):
        client = self.invoke("qualify-roles", "invalid JSON")
        client.request.assert_called_once_with("/v4/qualify-roles", {})
