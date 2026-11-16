import copy
import subprocess
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from .client import InvalidResponse
from .processes import ProcessTracker, current_tracker, group_alive, popen
from .recovery import ReconciliationRequired
from .release import ControlledRelease
from .test_recovery import RecoveryFixture

REPOSITORY = "https://github.com/example/controlled-test"


class ControlledRecoveryTests(RecoveryFixture, unittest.TestCase):
    def setUp(self):
        self.create_recovery()
        disk = patch("shutil.disk_usage", return_value=SimpleNamespace(free=10**10))
        disk.start()
        self.addCleanup(disk.stop)
        self.state["controller_secret"] = "a" * 64
        self.harness = ControlledRelease(
            self.client, self.journal, str(uuid4()), binding=lambda: {}
        )
        self.harness.authorization = lambda: {"repository_url": REPOSITORY}
        self.client.active = lambda: {
            "run": (
                {"id": self.client.remote["run"]["id"]}
                if self.client.remote and self.client.remote["run"]["state"] == "active"
                else None
            )
        }

    def controlled_pending(self):
        self.pending_claim()
        self.state["operation"] = None
        self.state["release_claim"] = {"run_id": self.run_id, "item_id": 14}
        self.client.remote = {
            "run": {"id": self.run_id, "state": "active"},
            "item": {"id": 14, "repository_url": REPOSITORY},
        }
        request = self.client.request

        def controlled_request(method, path, payload=None):
            if path == self.harness.path + "claim/":
                self.client.calls.append((method, path, copy.deepcopy(payload)))
                if self.client.remote is None:
                    self.client.remote = {
                        "run": {"id": payload["run_id"], "state": "active"},
                        "item": {
                            "id": payload["item_id"],
                            "repository_url": REPOSITORY,
                        },
                    }
                result = copy.deepcopy(self.client.remote)
                result["item"]["claim_token"] = "original-controlled-claim"
                return result
            return request(method, path, payload)

        self.client.request = controlled_request
        self.journal.save(self.state)

    def owned_controlled(self):
        self.owned()
        for result in (self.state["run"], self.client.remote):
            result["item"].update(id=14, repository_url=REPOSITORY)
        self.journal.save(self.state)

    def test_lost_controlled_claim_response_recovers_original_uuid_and_counts_once(
        self,
    ):
        self.controlled_pending()
        self.harness.recover(self.state, self.gates)
        self.harness.recover(self.state, self.gates)
        self.assertEqual(self.state["run"]["run"]["id"], self.run_id)
        self.assertEqual(
            self.state["run"]["item"]["claim_token"], "original-controlled-claim"
        )
        self.assertIsNone(self.state["release_claim"])
        self.assertEqual(sum(self.state["gates"]["counts"].values()), 1)
        claims = [call for call in self.client.calls if call[1].endswith("claim/")]
        self.assertEqual(
            claims,
            [
                (
                    "POST",
                    self.harness.path + "claim/",
                    {"run_id": self.run_id, "item_id": 14},
                )
            ],
        )

    def test_existing_claim_metadata_recovers_under_disk_pressure(self):
        self.controlled_pending()
        with patch("shutil.disk_usage", return_value=SimpleNamespace(free=0)):
            self.harness.recover(self.state, self.gates)
        self.assertEqual(self.state["run"]["run"]["id"], self.run_id)
        self.assertEqual(
            self.state["run"]["item"]["claim_token"], "original-controlled-claim"
        )

    def test_absent_controlled_claim_preserves_original_intent_without_creating_work(
        self,
    ):
        self.controlled_pending()
        self.client.remote = None
        self.harness.recover(self.state, self.gates)
        self.assertEqual(self.state["release_claim"]["run_id"], self.run_id)
        self.assertEqual(self.state["gates"]["pending_claim"]["run_id"], self.run_id)
        self.assertNotIn("run", self.state)
        self.assertFalse(any(call[1].endswith("claim/") for call in self.client.calls))

    def test_unavailable_claim_lookup_preserves_original_intent(self):
        self.controlled_pending()
        original = copy.deepcopy(self.state["release_claim"])
        self.client.reconcile = lambda run_id: (_ for _ in ()).throw(
            ConnectionError("offline")
        )
        with self.assertRaises(ConnectionError):
            self.harness.recover(self.state, self.gates)
        self.assertEqual(self.state["release_claim"], original)
        self.assertFalse(any(call[1].endswith("claim/") for call in self.client.calls))

    def test_wrong_controlled_item_cannot_recover_claim_token(self):
        self.controlled_pending()
        self.client.remote["item"]["id"] = 99
        with self.assertRaises(ReconciliationRequired):
            self.harness.recover(self.state, self.gates)
        self.assertFalse(any(call[1].endswith("claim/") for call in self.client.calls))

    def test_ordinary_pending_claim_cannot_be_repeated_by_controlled_runner(self):
        self.pending_claim()
        with self.assertRaises(ReconciliationRequired):
            self.harness.recover(self.state, self.gates)
        self.assertFalse(any(call[1].endswith("claim/") for call in self.client.calls))

    def test_remote_owned_run_without_local_proof_blocks_before_backend_probe(self):
        self.client.remote = {
            "run": {"id": self.run_id, "state": "active"},
            "item": {"repository_url": REPOSITORY},
        }
        with self.assertRaises(ReconciliationRequired):
            self.harness.recover(self.state, self.gates)
        self.assertFalse(any("secrets" in call[1] for call in self.client.calls))

    def test_surviving_worker_stops_before_uncertain_model_blocks_restart(self):
        self.owned_controlled()
        tracker = ProcessTracker(self.journal, self.state)
        context = current_tracker.set(tracker)
        try:
            child = popen(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
            )
        finally:
            current_tracker.reset(context)
        self.addCleanup(child.wait, 3)
        self.state["implementation"] = {self.run_id: {"model_pending": True}}
        self.journal.save(self.state)
        with self.assertRaisesRegex(ReconciliationRequired, "uncertain"):
            self.harness.recover(self.state, self.gates)
        self.assertFalse(group_alive(child.pid))
        self.assertTrue(
            all(row["status"] == "stopped" for row in self.state["processes"].values())
        )
        self.assertFalse(any("secrets" in call[1] for call in self.client.calls))

    def test_completed_reporting_preserves_local_claim_proof_without_execution(self):
        self.owned_controlled()
        claim = self.state["run"]["item"]["claim_token"]
        self.state["delivery"] = {"phase": "reporting_pending"}
        self.client.remote["run"]["state"] = "completed"
        self.client.remote["item"].pop("claim_token")
        result = self.harness.recover(self.state, self.gates, execution=False)
        self.assertEqual(result["run"]["state"], "completed")
        self.assertEqual(self.state["run"]["item"]["claim_token"], claim)
        self.assertIsNone(self.state["remote_active_run_id"])
        self.assertEqual(self.state["delivery"]["phase"], "reporting_pending")
        self.assertFalse(any("secrets" in call[1] for call in self.client.calls))

    def test_crash_after_initial_gate_save_preserves_controlled_claim_intent_atomically(
        self,
    ):
        from unittest.mock import patch

        original_save = self.journal.save

        def crash_after_prepared(state):
            original_save(state)
            if state.get("gates", {}).get("pending_claim"):
                raise RuntimeError("crash after durable gate preparation")

        with (
            patch.object(self.journal, "save", side_effect=crash_after_prepared),
            self.assertRaises(RuntimeError),
        ):
            self.harness.claim(self.state, 14)
        restored = self.journal.load()
        self.assertEqual(
            restored["release_claim"]["run_id"],
            restored["gates"]["pending_claim"]["run_id"],
        )
        self.harness.recover(restored, self.gates)
        self.assertEqual(
            restored["release_claim"]["run_id"],
            restored["gates"]["pending_claim"]["run_id"],
        )
        self.assertFalse(any(call[1].endswith("claim/") for call in self.client.calls))

    def test_crash_during_absent_claim_retry_preserves_uuid_until_idempotent_delivery(
        self,
    ):
        from unittest.mock import patch

        self.controlled_pending()
        self.client.remote = None
        self.harness.recover(self.state, self.gates)
        original_save = self.journal.save

        def crash_after_gate_check(state):
            original_save(state)
            raise RuntimeError("crash after durable retry gate check")

        with (
            patch.object(self.journal, "save", side_effect=crash_after_gate_check),
            self.assertRaises(RuntimeError),
        ):
            self.harness.claim(self.state, 14)
        restored = self.journal.load()
        self.assertEqual(restored["release_claim"]["run_id"], self.run_id)
        self.harness.recover(restored, self.gates)
        self.harness.claim(restored, 14)
        self.assertEqual(restored["run"]["run"]["id"], self.run_id)
        self.assertEqual(sum(restored["gates"]["counts"].values()), 1)
        claims = [call for call in self.client.calls if call[1].endswith("claim/")]
        self.assertEqual(len(claims), 1)
        self.assertEqual(claims[0][2]["run_id"], self.run_id)

    def assert_controlled_retry_blocked(self, reason):
        self.controlled_pending()
        self.client.remote = None
        self.harness.recover(self.state, self.gates)
        with self.assertRaisesRegex(InvalidResponse, "gates block"):
            self.harness.claim(self.state, 14)
        self.assertEqual(self.state["release_claim"]["run_id"], self.run_id)
        self.assertIn(reason, self.state["gates"]["checkpoint"]["reasons"])
        self.assertFalse(any(call[1].endswith("claim/") for call in self.client.calls))

    def test_controlled_retry_observes_new_pause(self):
        self.client.configuration_document["global"]["paused"] = True
        self.assert_controlled_retry_blocked("paused")

    def test_controlled_retry_observes_new_schedule(self):
        self.client.configuration_document["global"].update(
            schedule_mode="windows", windows=[]
        )
        self.assert_controlled_retry_blocked("outside_window")

    def test_controlled_retry_requires_fresh_usage(self):
        self.client.configuration_document["global"]["reserve_enabled"] = True
        self.assert_controlled_retry_blocked("usage_missing")


class NormalClaimRetryGateTests(RecoveryFixture, unittest.TestCase):
    def setUp(self):
        self.create_recovery()
        self.pending_claim()

    def assert_blocked(self, reason):
        original = copy.deepcopy(self.state["operation"])
        with self.assertRaisesRegex(ReconciliationRequired, "processing gates"):
            self.recovery.reconcile(self.state)
        self.assertEqual(self.state["operation"], original)
        self.assertEqual(self.state["gates"]["pending_claim"]["run_id"], self.run_id)
        self.assertIn(reason, self.state["gates"]["checkpoint"]["reasons"])
        self.assertFalse(any(call[1].endswith("claim/") for call in self.client.calls))

    def test_normal_pending_claim_cannot_retry_outside_current_schedule(self):
        self.client.configuration_document["global"].update(
            schedule_mode="windows", windows=[]
        )
        self.assert_blocked("outside_window")

    def test_normal_pending_claim_cannot_retry_without_fresh_usage(self):
        self.client.configuration_document["global"]["reserve_enabled"] = True
        self.assert_blocked("usage_missing")

    def test_normal_pending_claim_cannot_retry_while_paused(self):
        self.client.configuration_document["global"]["paused"] = True
        self.assert_blocked("paused")

    def test_retry_context_cannot_ignore_another_pending_claim(self):
        from .journal import JournalError

        with self.assertRaises(JournalError):
            self.gates.evaluate(
                self.client.configuration(), self.state, retry_claim_id=str(uuid4())
            )
        self.assertEqual(self.state["operation"]["run_id"], self.run_id)


class ProcessExitRaceTests(unittest.TestCase):
    def test_process_exiting_during_stat_read_is_already_stopped(self):
        from unittest.mock import patch
        from .processes import identity

        with patch(
            "coding_runner.processes.Path.read_text", side_effect=ProcessLookupError
        ):
            self.assertIsNone(identity(123))
