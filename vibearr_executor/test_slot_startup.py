from datetime import datetime, timedelta, timezone
import threading
import time
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from .client import ExecutorRejected, ExecutorUnavailable
from .compact_worker import CompactHandler
from .contracts import Rejected
from .runtime import durable
from . import test_compact_protocol as protocol_tests


class SlotStartupTests(unittest.TestCase):
    setUp = protocol_tests.CompactWriteProtocolTests.setUp
    endpoint = protocol_tests.CompactWriteProtocolTests.endpoint

    def prepare_operation(self):
        runtime = self.compact
        operation = self.manifest["operation_id"]
        original = runtime.prepare(self.manifest)
        durable(
            runtime.path(operation) / "backend-secret", b"metadata-only test credential"
        )
        return operation, original

    def hold_preparation(self, entered):
        def prepare(operation, profile, record):
            entered.set()
            if not self.compact.cancel.wait(5):
                raise RuntimeError("Test preparation was not canceled.")
            raise Rejected("Preparation canceled before candidate launch.")

        return prepare

    def test_http_controls_remain_responsive_and_wrong_stop_preserves_owner(self):
        runtime = self.compact
        operation, original = self.prepare_operation()
        other = str(uuid4())
        runtime.prepare({**self.manifest, "operation_id": other})
        entered = threading.Event()
        with (
            patch("vibearr_executor.slots.terminate_children"),
            patch.object(runtime, "capabilities", return_value={"qualified": True}),
            patch.object(
                runtime, "prepare_phase", side_effect=self.hold_preparation(entered)
            ) as prepare,
            patch.object(runtime, "reset"),
            patch("vibearr_executor.slots.subprocess.Popen") as process,
            self.endpoint(runtime, CompactHandler) as client,
        ):
            client.timeout = 1
            started = client.start(operation)
            self.assertEqual(
                started["execution_identity"], original["execution_identity"]
            )
            self.assertTrue(entered.wait(1))
            self.assertEqual(client.status(operation)["operation_id"], operation)
            self.assertEqual(client.start(operation), started)
            with self.assertRaises(ExecutorRejected):
                client.start(other)
            stopped_other = client.stop(other)
            self.assertEqual(stopped_other["operation_id"], other)
            self.assertTrue(stopped_other["result"]["canceled"])
            self.assertFalse(runtime.cancel.is_set())
            self.assertEqual(runtime.active, operation)
            with self.assertRaises(ExecutorRejected):
                client.start(str(uuid4()))
            stopped = client.stop(operation)
            self.assertEqual(stopped["state"], "exited")
            self.assertTrue(stopped["result"]["canceled"])
            self.assertTrue(stopped["descendants_terminated"])
            self.assertEqual(
                stopped["execution_identity"], original["execution_identity"]
            )
            self.assertEqual(client.start(operation), stopped)
            prepare.assert_called_once()
            process.assert_not_called()
        self.assertEqual(runtime.active, "")
        self.assertFalse(runtime.fenced)
        self.assertIs(runtime.record(operation)["task_launch_intent"], False)

    def test_preparation_deadline_seals_original_operation_without_launch(self):
        runtime = self.compact
        self.manifest["deadline"] = (
            datetime.now(timezone.utc) + timedelta(seconds=0.3)
        ).isoformat()
        operation, original = self.prepare_operation()

        def prepare(selected, profile, record):
            while True:
                runtime.check_preparation(record)
                time.sleep(0.01)

        with (
            patch("vibearr_executor.slots.terminate_children"),
            patch.object(runtime, "capabilities", return_value={"qualified": True}),
            patch.object(runtime, "prepare_phase", side_effect=prepare),
            patch.object(runtime, "reset"),
            patch("vibearr_executor.slots.subprocess.Popen") as process,
        ):
            runtime.start(operation)
            runtime.thread.join(timeout=2)
            process.assert_not_called()
        result = runtime.status(operation)
        self.assertEqual(result["state"], "exited")
        self.assertTrue(result["result"]["timed_out"])
        self.assertFalse(result["result"]["canceled"])
        self.assertEqual(result["execution_identity"], original["execution_identity"])
        self.assertFalse(runtime.fenced)

    def test_lost_start_response_reuses_the_original_preparation(self):
        runtime = self.compact
        operation, original = self.prepare_operation()
        entered = threading.Event()
        with (
            patch("vibearr_executor.slots.terminate_children"),
            patch.object(runtime, "capabilities", return_value={"qualified": True}),
            patch.object(
                runtime, "prepare_phase", side_effect=self.hold_preparation(entered)
            ) as prepare,
            patch.object(runtime, "reset"),
            self.endpoint(runtime, CompactHandler) as client,
        ):
            request = client.request

            def lose_response(path, data=None):
                request(path, data)
                raise ExecutorUnavailable("Response lost after accepted start.")

            with patch.object(client, "request", side_effect=lose_response):
                with self.assertRaises(ExecutorUnavailable):
                    client.start(operation)
            self.assertTrue(entered.wait(1))
            self.assertEqual(
                client.start(operation)["execution_identity"],
                original["execution_identity"],
            )
            self.assertTrue(client.stop(operation)["descendants_terminated"])
            prepare.assert_called_once()

    def test_changed_prepared_boundary_cannot_claim_preparation(self):
        runtime = self.compact
        operation, _ = self.prepare_operation()
        record = runtime.record(operation)
        record["execution_identity"]["binding_digest"] = "f" * 64
        runtime.save(record)
        with (
            patch.object(runtime, "capabilities", return_value={"qualified": True}),
            patch.object(runtime, "prepare_phase") as prepare,
        ):
            with self.assertRaises(Rejected):
                runtime.start(operation)
        prepare.assert_not_called()
        self.assertEqual(runtime.record(operation)["state"], "prepared")
        self.assertEqual(runtime.active, "")

    def test_preparation_failure_is_terminal_and_retry_does_not_launch_again(self):
        runtime = self.compact
        operation, original = self.prepare_operation()
        with (
            patch("vibearr_executor.slots.terminate_children"),
            patch.object(runtime, "capabilities", return_value={"qualified": True}),
            patch.object(
                runtime, "prepare_phase", side_effect=RuntimeError("failed startup")
            ) as prepare,
            patch.object(runtime, "reset"),
        ):
            runtime.start(operation)
            runtime.thread.join(timeout=2)
            terminal = runtime.status(operation)
            self.assertEqual(runtime.start(operation), terminal)
            prepare.assert_called_once()
        self.assertEqual(terminal["state"], "exited")
        self.assertTrue(terminal["result"]["export_error"])
        self.assertEqual(terminal["execution_identity"], original["execution_identity"])

    def test_thread_start_failure_fences_without_preparation_or_relaunch(self):
        runtime = self.compact
        operation, _ = self.prepare_operation()
        with (
            patch.object(runtime, "capabilities", return_value={"qualified": True}),
            patch("threading.Thread.start", side_effect=RuntimeError("PID limit")),
        ):
            with self.assertRaises(Rejected):
                runtime.start(operation)
        self.assertEqual(runtime.status(operation)["state"], "unknown")
        self.assertTrue(runtime.fenced)
        self.assertIsNone(runtime.thread)

    def test_database_preparation_observes_cancel_and_operation_deadline(self):
        runtime = self.compact
        runtime.database = Mock()
        future = time.time() + 10
        runtime.prepare_database(str(uuid4()), future)
        cancel = runtime.database.prepare.call_args.kwargs["cancel"]
        self.assertFalse(cancel.is_set())
        runtime.cancel.set()
        self.assertTrue(cancel.is_set())
        runtime.cancel.clear()
        runtime.prepare_database(str(uuid4()), time.time() - 1)
        self.assertTrue(runtime.database.prepare.call_args.kwargs["cancel"].is_set())
