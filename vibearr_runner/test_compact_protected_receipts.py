from copy import deepcopy
import unittest

from .compact_evidence import digest, protected_operation, validate_payload


class ProtectedEngineeringReceiptTests(unittest.TestCase):
    def setUp(self):
        self.worker = {
            "runtime": "compact-worker-v1",
            "protocol_version": 2,
            "binding_digest": "b" * 64,
            "instance_id": "01234567-89ab-4cde-8f01-23456789abcd",
            "worker_boot": "12345678-9abc-4def-8012-3456789abcde",
        }
        operation = "23456789-abcd-4ef0-8123-456789abcdef"
        run = "3456789a-bcde-4f01-8234-56789abcdef0"
        self.record = {
            "operation_id": operation,
            "state": "exited",
            "descendants_terminated": True,
            "worker_boot": self.worker["worker_boot"],
            "execution_identity": dict(self.worker),
            "manifest": {"operation_id": operation, "run_id": run},
            "result": {
                "operation_id": operation,
                "run_id": run,
                "exit_code": 0,
                "execution_identity": dict(self.worker),
                "terminated": True,
                "timed_out": False,
                "canceled": False,
                "log_truncated": False,
                "started_at": 1700000000.0,
                "ended_at": 1700000001.0,
            },
        }
        self.seal(self.record)

    def seal(self, record):
        record["receipt_digest"] = digest(
            {
                "manifest": record["manifest"],
                "result": record["result"],
                "boot": record["worker_boot"],
            }
        )

    def assert_failed_result_rejected(self, key, value):
        self.record["result"][key] = value
        self.seal(self.record)
        with self.assertRaises(ValueError):
            protected_operation(self.record, self.worker)

    def test_consistent_successful_operation_is_accepted(self):
        manifest, result = protected_operation(self.record, self.worker)
        self.assertEqual(manifest, self.record["manifest"])
        self.assertEqual(result, self.record["result"])

    def test_record_boot_must_match_current_execution_identity(self):
        self.record["worker_boot"] = "456789ab-cdef-4012-8345-6789abcdef01"
        self.seal(self.record)
        with self.assertRaises(ValueError):
            protected_operation(self.record, self.worker)

    def test_boolean_exit_code_cannot_claim_integer_success(self):
        self.assert_failed_result_rejected("exit_code", False)

    def test_canceled_operation_cannot_claim_success(self):
        self.assert_failed_result_rejected("canceled", True)

    def test_timed_out_operation_cannot_claim_success(self):
        self.assert_failed_result_rejected("timed_out", True)

    def test_failed_export_cannot_claim_success(self):
        self.assert_failed_result_rejected("export_error", "failed export")

    def test_start_after_sealed_end_is_rejected(self):
        self.assert_failed_result_rejected("started_at", 1700000002.0)

    def test_missing_sealed_end_is_rejected(self):
        self.record["result"].pop("ended_at")
        self.seal(self.record)
        with self.assertRaisesRegex(ValueError, "chronology"):
            protected_operation(self.record, self.worker)

    def test_boolean_sealed_end_is_rejected(self):
        self.assert_failed_result_rejected("ended_at", True)

    def test_negative_sealed_start_is_rejected(self):
        self.assert_failed_result_rejected("started_at", -1)

    def test_running_restart_cannot_claim_reconciled_execution(self):
        record = deepcopy(self.record)
        record.update(
            state="running", worker_boot="456789ab-cdef-4012-8345-6789abcdef01"
        )
        record["result"].update(
            worker_restart=True,
            reconciled_boot=self.worker["worker_boot"],
            artifact={"sha256": "a" * 64},
        )
        self.seal(record)
        payload = {"operation": record, "restart_receipt": record["receipt_digest"]}
        with self.assertRaises(ValueError):
            validate_payload("worker_restart", payload, "c" * 40, self.worker, {})
