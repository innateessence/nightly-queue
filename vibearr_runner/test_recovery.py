import copy
from pathlib import Path
import sys
import tempfile
import unittest
from uuid import uuid4
from unittest.mock import patch

from .client import AmbiguousRequest, NotFoundError
from .controller import Controller
from .gates import GateEvaluator
from .journal import ControllerLocked, Journal
from .processes import ProcessTracker, current_tracker, popen, reaped, group_alive
from .recovery import DurableClient, RecoveryCoordinator, ReconciliationRequired
from .test_gates import CONFIG, NOW


class Queue:
    def __init__(self):
        self.calls, self.results = [], {}
        self.remote, self.claim_response = None, None
        self.lost = False
        self.configuration_document = copy.deepcopy(CONFIG)

    def request(self, method, path, payload=None, **limits):
        self.calls.append((method, path, copy.deepcopy(payload)))
        if method == "GET" and ("/operations/" in path or "/handoff/" in path):
            operation = path.rstrip("/").split("/")[-1]
            if operation not in self.results:
                raise NotFoundError()
            return (
                {"response": self.results[operation]}
                if "/operations/" in path
                else self.results[operation]
            )
        if path.endswith("/runs/claim/"):
            if self.claim_response is None:
                self.claim_response = {
                    "run": {"id": payload["run_id"], "state": "active"},
                    "item": {"claim_token": "claim"},
                }
                self.remote = copy.deepcopy(self.claim_response)
            result = self.claim_response
        elif payload and "operation_id" in payload:
            result = self.results.setdefault(
                payload["operation_id"], {"context": {"id": str(uuid4())}}
            )
        else:
            result = {}
        if self.lost:
            self.lost = False
            raise AmbiguousRequest()
        return result

    def reconcile(self, run_id):
        if self.remote is None or self.remote["run"]["id"] != run_id:
            raise NotFoundError()
        return copy.deepcopy(self.remote)

    def configuration(self):
        return copy.deepcopy(self.configuration_document)

    def gate_report(self, report):
        return {}


class RecoveryFixture:
    def create_recovery(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "journal"
        self.journal = Journal(self.path)
        self.journal.__enter__()
        self.addCleanup(self.journal.__exit__)
        self.state = self.journal.load()
        self.client = Queue()
        self.gates = GateEvaluator(self.journal, clock=lambda: NOW)
        self.recovery = RecoveryCoordinator(self.client, self.journal, self.gates)
        self.run_id = str(uuid4())

    def pending_claim(self):
        decision = self.gates.evaluate(copy.deepcopy(CONFIG), self.state)
        self.gates.prepare_claim(self.state, self.run_id, decision)
        self.journal.prepare(
            self.state,
            self.run_id,
            "claim",
            {"run_id": self.run_id, "configuration_revision": "test"},
        )

    def owned(self):
        self.pending_claim()
        result = self.client.request(
            "POST", "/api/v1/runner/runs/claim/", self.state["operation"]["payload"]
        )
        self.recovery.accept_claim(self.state, result)
        return result


class RecoveryTests(RecoveryFixture, unittest.TestCase):
    def setUp(self):
        self.create_recovery()

    def test_loss_before_claim_reconciles_absence_then_claims_original_uuid(self):
        self.pending_claim()
        self.recovery.reconcile(self.state)
        self.assertEqual(self.state["run"]["run"]["id"], self.run_id)
        claims = [
            call for call in self.client.calls if call[1].endswith("/runs/claim/")
        ]
        self.assertEqual(len(claims), 1)
        self.assertEqual(sum(self.state["gates"]["counts"].values()), 1)

    def test_uncommitted_claim_is_not_replayed_when_current_controls_close(self):
        self.pending_claim()
        original = copy.deepcopy(self.state["operation"])
        self.client.configuration_document["global"]["paused"] = True
        with self.assertRaisesRegex(ReconciliationRequired, "processing gates"):
            self.recovery.reconcile(self.state)
        self.assertEqual(self.state["operation"], original)
        self.assertEqual(self.state["gates"]["pending_claim"]["run_id"], self.run_id)
        self.assertEqual(self.client.calls, [])
        self.assertEqual(self.state["gates"]["counts"], {})

    def test_uncommitted_claim_cannot_retry_outside_current_schedule(self):
        self.pending_claim()
        self.client.configuration_document["global"].update(
            schedule_mode="windows", windows=[]
        )
        with self.assertRaisesRegex(ReconciliationRequired, "processing gates"):
            self.recovery.reconcile(self.state)
        self.assertIn("outside_window", self.state["gates"]["checkpoint"]["reasons"])
        self.assertEqual(self.client.calls, [])

    def test_uncommitted_claim_requires_current_and_original_account_usage(self):
        from .gates import UsageReading, UsageWindow

        self.pending_claim()
        original_account = {"backend": "original", "credential_id": 99}
        self.state["gates"]["pending_claim"]["accounts"] = [original_account]
        self.client.configuration_document["global"]["reserve_enabled"] = True

        class Available:
            def read_usage(self, account, now):
                return UsageReading(True, (UsageWindow("quota", 100, now),))

        for account in CONFIG["accounts"]:
            self.gates.adapters[(account["backend"], account["credential_id"])] = (
                Available()
            )
        with self.assertRaisesRegex(ReconciliationRequired, "processing gates"):
            self.recovery.reconcile(self.state)
        self.assertIn("usage_missing", self.state["gates"]["checkpoint"]["reasons"])
        self.assertEqual(self.client.calls, [])

    def test_retry_gate_cannot_ignore_a_different_pending_claim(self):
        from .journal import JournalError

        self.pending_claim()
        with self.assertRaises(JournalError):
            self.gates.evaluate(
                self.client.configuration(), self.state, retry_claim_id=str(uuid4())
            )
        self.assertEqual(self.state["operation"]["run_id"], self.run_id)

    def test_paused_retry_can_resume_original_intent_and_count_once(self):
        self.pending_claim()
        original = copy.deepcopy(self.state["operation"])
        self.client.configuration_document["global"]["paused"] = True
        with self.assertRaises(ReconciliationRequired):
            self.recovery.reconcile(self.state)
        self.client.configuration_document["global"]["paused"] = False
        self.recovery.reconcile(self.journal.load())
        self.assertEqual(self.client.calls[-1][2], original["payload"])
        self.assertEqual(sum(self.journal.load()["gates"]["counts"].values()), 1)

    def test_loss_after_claim_never_consumes_a_second_item(self):
        self.pending_claim()
        self.client.request(
            "POST", "/api/v1/runner/runs/claim/", self.state["operation"]["payload"]
        )
        self.state["remote_active_run_id"] = self.run_id
        self.recovery.reconcile(self.state)
        self.recovery.reconcile(self.state)
        self.assertEqual(len(self.client.calls), 1)
        self.assertEqual(sum(self.state["gates"]["counts"].values()), 1)

    def test_crash_after_counter_write_never_charges_window_twice(self):
        self.pending_claim()
        result = self.client.request(
            "POST", "/api/v1/runner/runs/claim/", self.state["operation"]["payload"]
        )
        original_save = self.journal.save

        def crash_after_count(state):
            original_save(state)
            if state["gates"]["attempts"].get(self.run_id) and state.get("operation"):
                raise RuntimeError("Terminated after durable accounting")

        with patch.object(self.journal, "save", side_effect=crash_after_count):
            with self.assertRaises(RuntimeError):
                self.recovery.accept_claim(self.state, result)
        restored = self.journal.load()
        self.recovery.reconcile(restored)
        self.assertEqual(sum(restored["gates"]["counts"].values()), 1)
        self.assertIsNone(restored["operation"])

    def test_empty_claim_acceptance_is_idempotent(self):
        self.pending_claim()
        result = {"run": {"id": self.run_id, "state": "empty"}, "item": None}
        self.recovery.accept_claim(self.state, result)
        self.recovery.accept_claim(self.state, result)
        self.assertIsNone(self.state["gates"]["pending_claim"])
        self.assertEqual(self.state["gates"]["counts"], {})

    def test_termination_between_gate_preparation_and_claim_intent_is_safe(self):
        decision = self.gates.evaluate(copy.deepcopy(CONFIG), self.state)
        self.gates.prepare_claim(self.state, self.run_id, decision)
        self.recovery.reconcile(self.state)
        self.assertIsNone(self.state["gates"]["pending_claim"])
        self.assertEqual(self.state["gates"]["counts"], {})
        self.assertEqual(self.client.calls, [])

    def test_missing_journal_never_adopts_remote_run(self):
        self.state["remote_active_run_id"] = self.run_id
        with self.assertRaises(ReconciliationRequired):
            self.recovery.bind(self.state)
        self.assertEqual(self.client.calls, [])

    def test_existing_proof_without_matching_run_does_not_adopt_remote_work(self):
        self.state["controller_secret"] = "a" * 64
        self.state["remote_active_run_id"] = self.run_id
        with self.assertRaises(ReconciliationRequired):
            self.recovery.reconcile(self.state)
        self.assertEqual(self.client.calls, [])

    def test_owned_model_uncertainty_requires_operator(self):
        self.owned()
        self.state["implementation"] = {self.run_id: {"model_pending": True}}
        with self.assertRaisesRegex(ReconciliationRequired, "uncertain outcome"):
            self.recovery.reconcile(self.state)
        self.assertEqual(len(self.client.calls), 1)

    def test_validation_started_before_crash_is_not_reexecuted(self):
        self.owned()
        self.state["implementation"] = {self.run_id: {"pending": {"started": True}}}
        with self.assertRaises(ReconciliationRequired):
            self.recovery.reconcile(self.state)

    def test_interrupted_budget_is_never_reset(self):
        self.owned()
        self.state["budgets"] = {
            self.run_id: {"active": {"stage": "validation", "started": 1}, "total": 12}
        }
        with self.assertRaises(ReconciliationRequired):
            self.recovery.reconcile(self.state)
        self.assertEqual(self.state["budgets"][self.run_id]["total"], 12)

    def test_obsolete_run_cannot_resume(self):
        self.owned()
        self.client.remote["run"]["state"] = "obsolete"
        with self.assertRaises(ReconciliationRequired):
            self.recovery.reconcile(self.state)

    def test_owned_work_retains_original_window_count_after_restart(self):
        self.owned()
        original = copy.deepcopy(self.state["gates"]["counts"])
        self.recovery.reconcile(self.journal.load())
        self.assertEqual(self.journal.load()["gates"]["counts"], original)

    def test_global_pause_is_rechecked_before_owned_execution(self):
        self.owned()
        self.client.configuration_document["global"]["paused"] = True
        controller = Controller(self.client, self.journal, gates=self.gates)
        controller.state = self.state
        self.assertFalse(controller.checkpoint())
        self.assertEqual(self.state["gates"]["checkpoint"]["reasons"], ["paused"])

    def test_cancellation_stops_checkpoint_before_model_work(self):
        self.owned()
        self.client.remote["run"]["cancellation_requested_at"] = "now"
        controller = Controller(self.client, self.journal, gates=self.gates)
        controller.state = self.state
        self.assertFalse(controller.checkpoint())

    def test_completed_remote_run_preserves_claim_for_reporting_ack(self):
        self.owned()
        self.client.remote["run"]["state"] = "completed"
        self.client.remote["item"] = {"id": 1}
        self.recovery.reconcile(self.state)
        self.assertEqual(self.state["run"]["item"]["claim_token"], "claim")

    def test_completed_callback_receives_private_claim_after_lost_finish(self):
        self.owned()
        self.state["controller_secret"] = "a" * 64
        self.client.remote["run"].update(state="completed", finish_digest="f" * 64)
        self.client.remote["item"] = {"id": 1}
        callbacks = []

        def completed(state, run_id, token, finish_digest):
            callbacks.append((run_id, token, finish_digest))

        controller = Controller(
            self.client, self.journal, gates=self.gates, completed=completed
        )
        self.assertEqual(
            controller.cycle(self.state, copy.deepcopy(CONFIG)), "completed"
        )
        self.assertEqual(callbacks, [(self.run_id, "claim", "f" * 64)])
        self.assertNotIn("run", self.state)

    def test_same_volume_rejects_second_controller(self):
        with self.assertRaises(ControllerLocked):
            with Journal(self.path):
                pass


class MetadataRecoveryTests(RecoveryFixture, unittest.TestCase):
    def setUp(self):
        self.create_recovery()
        self.durable = DurableClient(self.client, self.journal, self.state)
        self.endpoint = f"/api/v1/runner/runs/{self.run_id}/contexts/"
        self.payload = {
            "claim_token": "claim",
            "operation_id": str(uuid4()),
            "document": {"title": "one"},
        }

    def test_recovery_lookup_and_replay_share_fresh_timeout_without_journaling_options(
        self,
    ):
        clock = [10.0]
        options = []
        request = self.client.request

        def bounded(method, path, payload=None, **limits):
            options.append(limits)
            clock[0] += 0.25
            return request(method, path, payload)

        self.state["request"] = {
            "path": self.endpoint,
            "payload": self.payload,
            "status": "pending",
        }
        with (
            patch(
                "vibearr_runner.recovery.time.monotonic", side_effect=lambda: clock[0]
            ),
            patch.object(self.client, "request", side_effect=bounded),
        ):
            self.durable.recover(timeout=1, checkpoint=lambda: True)
        self.assertEqual([entry["timeout"] for entry in options], [1, 0.75])
        saved = self.journal.load()["request"]
        self.assertEqual(set(saved), {"path", "payload", "status", "response"})
        self.assertEqual(saved["payload"], self.payload)

    def test_identical_metadata_retry_is_one_write(self):
        first = self.durable.request("POST", self.endpoint, self.payload)
        second = self.durable.request("POST", self.endpoint, self.payload)
        self.assertEqual(first, second)
        self.assertEqual(len(self.client.calls), 1)

    def test_new_operation_on_same_endpoint_creates_new_document(self):
        first = self.durable.request("POST", self.endpoint, self.payload)
        second = self.durable.request(
            "POST", self.endpoint, {**self.payload, "operation_id": str(uuid4())}
        )
        self.assertNotEqual(first, second)
        self.assertEqual(len(self.client.calls), 2)

    def test_same_operation_cannot_change_payload(self):
        self.durable.request("POST", self.endpoint, self.payload)
        with self.assertRaises(ReconciliationRequired):
            self.durable.request(
                "POST",
                self.endpoint,
                {**self.payload, "document": {"title": "changed"}},
            )

    def test_lost_upload_response_is_reconciled_without_another_upload(self):
        self.client.lost = True
        with self.assertRaises(AmbiguousRequest):
            self.durable.request("POST", self.endpoint, self.payload)
        restarted = DurableClient(self.client, self.journal, self.journal.load())
        result = restarted.request("POST", self.endpoint, self.payload)
        self.assertIn("context", result)
        self.assertEqual(
            [method for method, _, _ in self.client.calls], ["POST", "GET"]
        )

    def test_crash_before_upload_reconciles_absence_before_exact_replay(self):
        self.state["request"] = {
            "path": self.endpoint,
            "payload": self.payload,
            "status": "pending",
        }
        self.durable.recover()
        self.assertEqual(
            [method for method, _, _ in self.client.calls], ["GET", "POST"]
        )

    def test_pending_upload_is_not_replaced_by_new_operation(self):
        self.state["request"] = {
            "path": self.endpoint,
            "payload": self.payload,
            "status": "pending",
        }
        with self.assertRaises(ReconciliationRequired):
            self.durable.request(
                "POST", self.endpoint, {**self.payload, "operation_id": str(uuid4())}
            )
        self.assertEqual(self.state["request"]["payload"], self.payload)

    def test_metadata_loss_reconciles_each_endpoint(self):
        for endpoint in (
            "contexts",
            "plans",
            "approvals",
            "artifacts",
            "handoff",
            "progress",
        ):
            with self.subTest(endpoint=endpoint):
                self.state["request"] = None
                payload = {"claim_token": "claim", "operation_id": str(uuid4())}
                path = f"/api/v1/runner/runs/{self.run_id}/{endpoint}/"
                self.client.lost = True
                with self.assertRaises(AmbiguousRequest):
                    self.durable.request("POST", path, payload)
                self.durable.recover()
                calls = [
                    call
                    for call in self.client.calls
                    if call[0] == "POST" and call[1] == path
                ]
                self.assertEqual(len(calls), 1)


class ProcessRecoveryTests(RecoveryFixture, unittest.TestCase):
    def setUp(self):
        self.create_recovery()

    def spawn(self):
        tracker = ProcessTracker(self.journal, self.state)
        token = current_tracker.set(tracker)
        self.addCleanup(current_tracker.reset, token)
        child = popen([sys.executable, "-c", "import time; time.sleep(60)"], env={})
        self.addCleanup(child.wait)
        self.addCleanup(tracker.recover)
        return tracker, child

    def test_preparation_process_stops_at_aggregate_active_deadline(self):
        import time
        from .budgets import BudgetExceeded
        from .processes import current_execution, run

        tracker = ProcessTracker(self.journal, self.state)
        context = current_tracker.set(tracker)
        execution = current_execution.set(
            (time.monotonic() + 0.1, lambda: True, lambda: True)
        )
        started = time.monotonic()
        try:
            with self.assertRaises(BudgetExceeded):
                run(
                    [sys.executable, "-c", "import time; time.sleep(60)"],
                    capture_output=True,
                    timeout=120,
                )
        finally:
            current_execution.reset(execution)
            current_tracker.reset(context)
        self.assertLess(time.monotonic() - started, 0.7)
        self.assertTrue(
            all(row["status"] == "stopped" for row in self.state["processes"].values())
        )

    def test_preparation_process_stops_and_reaps_on_local_shutdown(self):
        import threading
        import time
        from .backends import BackendInterrupted
        from .processes import current_execution, run

        stopped = threading.Event()
        tracker = ProcessTracker(self.journal, self.state)
        context = current_tracker.set(tracker)
        execution = current_execution.set(
            (time.monotonic() + 10, lambda: not stopped.is_set(), lambda: True)
        )
        timer = threading.Timer(0.05, stopped.set)
        started = time.monotonic()
        try:
            timer.start()
            with self.assertRaises(BackendInterrupted):
                run(
                    [sys.executable, "-c", "import time; time.sleep(60)"],
                    capture_output=True,
                )
        finally:
            timer.cancel()
            current_execution.reset(execution)
            current_tracker.reset(context)
        self.assertLess(time.monotonic() - started, 0.7)
        self.assertTrue(
            all(row["status"] == "stopped" for row in self.state["processes"].values())
        )

    def test_execution_limits_without_tracker_still_stop_an_owned_process_group(self):
        import time
        from .budgets import BudgetExceeded
        from .processes import current_execution, run

        execution = current_execution.set(
            (time.monotonic() + 0.1, lambda: True, lambda: True)
        )
        started = time.monotonic()
        try:
            with self.assertRaises(BudgetExceeded):
                run(
                    [sys.executable, "-c", "import time; time.sleep(60)"],
                    capture_output=True,
                )
        finally:
            current_execution.reset(execution)
        self.assertLess(time.monotonic() - started, 0.7)

    def test_process_disappearing_during_stat_read_is_already_stopped(self):
        from .processes import identity

        with patch(
            "vibearr_runner.processes.Path.read_text", side_effect=ProcessLookupError
        ):
            self.assertIsNone(identity(123))

    def test_child_exiting_between_group_check_and_signal_is_confirmed(self):
        import os
        import signal

        tracker, child = self.spawn()
        signal_group = os.killpg

        def exited_before_signal(pid, requested_signal):
            signal_group(pid, signal.SIGKILL)
            child.wait()
            signal_group(pid, requested_signal)

        with patch(
            "vibearr_runner.processes.os.killpg", side_effect=exited_before_signal
        ):
            tracker.recover()
        self.assertFalse(group_alive(child.pid))
        self.assertEqual(
            next(iter(self.state["processes"].values()))["status"], "stopped"
        )

    def test_missing_signal_target_with_unconfirmed_group_stays_fenced(self):
        from .journal import JournalError

        tracker, child = self.spawn()
        with (
            patch("vibearr_runner.processes.os.killpg", side_effect=ProcessLookupError),
            patch("vibearr_runner.processes.group_alive", return_value=True),
            self.assertRaisesRegex(JournalError, "unconfirmed"),
        ):
            tracker.recover()
        self.assertEqual(
            next(iter(self.state["processes"].values()))["status"], "running"
        )
        tracker.recover()
        self.assertFalse(group_alive(child.pid))

    def test_restart_confirms_child_process_group_termination(self):
        _, child = self.spawn()
        saved = self.journal.load()
        ProcessTracker(self.journal, saved).recover()
        self.assertFalse(group_alive(child.pid))
        self.assertTrue(
            all(row["status"] == "stopped" for row in saved["processes"].values())
        )

    def test_process_is_recorded_before_trusted_command_executes(self):
        tracker, child = self.spawn()
        record = next(iter(self.journal.load()["processes"].values()))
        self.assertEqual(record["pid"], child.pid)
        self.assertEqual(record["status"], "running")
        self.assertGreater(record["start"], 0)
        tracker.recover()
        child.wait()
        reaped(child)
