import copy
from datetime import UTC, datetime, timedelta
from pathlib import Path
import tempfile
import threading
from unittest.mock import patch
from unittest import TestCase, main

from coding_runner.gates import GateEvaluator, UsageReading, UsageWindow, schedule
from coding_runner.journal import Journal, JournalError
from coding_runner.controller import Controller
from coding_runner.test_runtime import ServerCase


NOW = datetime(2026, 11, 16, 23, tzinfo=UTC)
ACCOUNT = {"backend": "fake", "credential_id": 1, "project_ids": [1]}
CONFIG = {
    "schema_version": 1,
    "revision": "test",
    "execution_ready": True,
    "global": {
        "enabled": True,
        "paused": False,
        "schedule_mode": "continuous",
        "timezone": "UTC",
        "windows": [],
        "tasks_per_window": 1,
        "reserve_enabled": False,
        "minimum_remaining_usage_percent": 40,
        "polling_interval_seconds": 60,
    },
    "accounts": [ACCOUNT],
    "projects": [],
}


class FakeUsage:
    def __init__(self, windows=(), supported=True):
        self.reading = UsageReading(supported, tuple(windows))
        self.calls = []

    def read_usage(self, account, now):
        self.calls.append(account)
        return self.reading


class GateTests(TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name) / "journal"
        self.journal = Journal(self.directory)
        self.journal.__enter__()
        self.addCleanup(self.temporary.cleanup)
        self.addCleanup(self.journal.__exit__)
        self.now = NOW
        self.config = copy.deepcopy(CONFIG)
        self.state = self.journal.load()
        self.gates = GateEvaluator(self.journal, clock=lambda: self.now)

    def evaluate(self, **kwargs):
        return self.gates.evaluate(self.config, self.state, **kwargs)

    def window(self, percent=80, observed=None, reset=None, identity="daily"):
        return UsageWindow(identity, percent, observed or self.now, reset)

    def usage(self, *windows, supported=True):
        self.config["global"]["reserve_enabled"] = True
        adapter = FakeUsage(windows, supported)
        self.gates.adapters[("fake", 1)] = adapter
        return adapter

    def claim(self, run_id="run"):
        decision = self.evaluate()
        self.gates.prepare_claim(self.state, run_id, decision)
        self.gates.record_claim(self.state, run_id)
        return decision

    def test_disabled_blocks_processing(self):
        self.config["global"]["enabled"] = False
        self.assertEqual(self.evaluate().reasons, ("disabled",))

    def test_pause_blocks_every_checkpoint(self):
        self.claim()
        self.config["global"]["paused"] = True
        for checkpoint in ("stage", "model", "periodic"):
            with self.subTest(checkpoint=checkpoint):
                self.assertEqual(
                    self.evaluate(checkpoint=checkpoint, run_id="run").reasons,
                    ("paused",),
                )

    def test_release_remains_disabled_without_verified_capability(self):
        self.config["execution_ready"] = False
        self.assertEqual(self.evaluate().reasons, ("release_disabled",))

    def test_capacity_prevents_new_claim(self):
        self.assertEqual(self.evaluate(active_count=1).reasons, ("capacity",))

    def test_owned_wait_resumes_before_any_new_claim(self):
        self.claim()
        self.config["global"]["paused"] = True
        self.evaluate(checkpoint="stage", run_id="run")
        self.config["global"]["paused"] = False
        self.assertIn("resume_owned_run_first", self.evaluate().reasons)
        self.assertTrue(
            self.evaluate(checkpoint="stage", run_id="run", active_count=1).allowed
        )
        self.assertEqual(self.state["gates"]["owned_run_id"], "run")

    def test_remote_owned_run_blocks_claim_after_restart(self):
        self.state["remote_active_run_id"] = "remote"
        self.assertIn("resume_owned_run_first", self.evaluate().reasons)

    def test_unowned_stage_is_blocked(self):
        self.assertEqual(
            self.evaluate(checkpoint="stage", run_id="other").reasons,
            ("ownership_unknown",),
        )

    def test_each_attempt_is_counted_once(self):
        decision = self.claim()
        self.gates.record_claim(self.state, "run")
        self.assertEqual(self.state["gates"]["counts"], {decision.interval["id"]: 1})
        self.gates.complete_run(self.state, "run")
        self.assertEqual(self.evaluate().reasons, ("window_limit",))

    def test_empty_claim_does_not_consume_capacity(self):
        decision = self.evaluate()
        self.gates.prepare_claim(self.state, "empty", decision)
        self.gates.abandon_claim(self.state, "empty")
        self.assertEqual(self.evaluate().tasks_started, 0)

    def test_claim_confirmation_uses_original_interval_after_midnight(self):
        decision = self.evaluate()
        self.gates.prepare_claim(self.state, "run", decision)
        self.now += timedelta(hours=2)
        self.gates.record_claim(self.state, "run")
        self.assertEqual(
            self.state["gates"]["attempts"]["run"], decision.interval["id"]
        )
        self.assertEqual(
            self.evaluate(checkpoint="stage", run_id="run").tasks_started, 0
        )

    def test_pending_claim_must_be_reconciled_before_another(self):
        decision = self.evaluate()
        self.gates.prepare_claim(self.state, "run", decision)
        with self.assertRaises(JournalError):
            self.gates.prepare_claim(self.state, "other", decision)

    def test_confirmation_without_original_interval_fails_closed(self):
        self.evaluate()
        with self.assertRaises(JournalError):
            self.gates.record_claim(self.state, "unknown")

    def test_counters_and_owned_wait_survive_restart(self):
        decision = self.claim()
        self.config["global"]["paused"] = True
        self.evaluate(checkpoint="model", run_id="run")
        self.now += timedelta(minutes=2)
        self.state = self.journal.load()
        self.gates = GateEvaluator(self.journal, clock=lambda: self.now)
        self.assertEqual(self.state["gates"]["counts"][decision.interval["id"]], 1)
        self.assertEqual(self.gates.paused_seconds(self.state, "run"), 120)
        self.config["global"]["paused"] = False
        self.assertTrue(self.evaluate(checkpoint="model", run_id="run").allowed)
        self.assertEqual(self.gates.paused_seconds(self.state, "run"), 120)

    def test_repeated_blocked_checks_do_not_restart_pause_timer(self):
        self.claim()
        self.config["global"]["paused"] = True
        self.evaluate(checkpoint="stage", run_id="run")
        self.now += timedelta(minutes=1)
        self.evaluate(checkpoint="stage", run_id="run")
        self.now += timedelta(minutes=1)
        self.assertEqual(self.gates.paused_seconds(self.state, "run"), 120)

    def test_new_global_controls_apply_to_owned_run(self):
        self.claim()
        self.config["global"]["paused"] = True
        self.config["revision"] = "changed"
        decision = self.evaluate(checkpoint="stage", run_id="run")
        self.assertEqual(decision.configuration_revision, "changed")
        self.assertIn("paused", decision.reasons)

    def test_continuous_limit_resets_at_local_midnight(self):
        self.config["global"]["timezone"] = "America/Los_Angeles"
        self.now = datetime(2026, 11, 17, 7, 59, tzinfo=UTC)
        previous = self.claim()
        self.gates.complete_run(self.state, "run")
        self.assertFalse(self.evaluate().allowed)
        self.now += timedelta(minutes=1)
        current = self.evaluate()
        self.assertTrue(current.allowed)
        self.assertNotEqual(previous.interval["id"], current.interval["id"])

    def test_disabled_reserve_removes_usage_gate(self):
        self.assertTrue(self.evaluate().allowed)
        self.assertEqual(self.evaluate().readings[0]["status"], "missing")

    def test_reserve_threshold_boundaries(self):
        for threshold in (0, 40, 100):
            for remaining in (0, 39, 40, 41, 99, 100):
                with self.subTest(threshold=threshold, remaining=remaining):
                    self.config["global"]["minimum_remaining_usage_percent"] = threshold
                    self.usage(self.window(remaining))
                    self.assertEqual(self.evaluate().allowed, remaining > threshold)

    def test_each_allowance_window_must_exceed_reserve(self):
        self.usage(self.window(80), self.window(40, identity="weekly"))
        decision = self.evaluate()
        self.assertEqual(decision.reasons, ("usage_reserve",))
        self.assertEqual(len(decision.readings[0]["windows"]), 2)

    def test_override_account_blocks_all_projects(self):
        self.usage(self.window(90))
        override = {"backend": "other", "credential_id": 7, "project_ids": [2]}
        self.config["accounts"].append(override)
        self.gates.adapters[("other", 7)] = FakeUsage([self.window(39)])
        self.assertEqual(self.evaluate().reasons, ("usage_reserve",))
        self.assertEqual(len(self.gates.adapters[("fake", 1)].calls), 1)

    def test_same_backend_different_accounts_do_not_average_or_switch(self):
        self.usage(self.window(90))
        self.config["accounts"].append({**ACCOUNT, "credential_id": 2})
        self.gates.adapters[("fake", 2)] = FakeUsage([self.window(1)])
        self.assertEqual(self.evaluate().reasons, ("usage_reserve",))

    def test_missing_adapter_blocks_when_enforced(self):
        self.config["global"]["reserve_enabled"] = True
        self.assertEqual(self.evaluate().reasons, ("usage_missing",))

    def test_empty_account_inventory_blocks_when_enforced(self):
        self.config["global"]["reserve_enabled"] = True
        self.config["accounts"] = []
        self.assertEqual(self.evaluate().reasons, ("usage_missing_accounts",))

    def test_unsupported_adapter_blocks(self):
        self.usage(supported=False)
        self.assertEqual(self.evaluate().reasons, ("usage_unsupported",))

    def test_empty_allowance_reading_blocks(self):
        self.usage()
        self.assertEqual(self.evaluate().reasons, ("usage_missing",))

    def test_stale_at_exact_age_boundary_blocks(self):
        self.usage(self.window(observed=self.now - timedelta(seconds=300)))
        self.assertEqual(self.evaluate().reasons, ("usage_stale",))

    def test_fresh_just_before_age_boundary_is_allowed(self):
        self.usage(self.window(observed=self.now - timedelta(seconds=299)))
        self.assertTrue(self.evaluate().allowed)

    def test_future_usage_timestamp_blocks(self):
        self.usage(self.window(observed=self.now + timedelta(seconds=1)))
        self.assertEqual(self.evaluate().reasons, ("usage_stale",))

    def test_invalid_usage_percent_and_units_block(self):
        for percent in (-1, 101, float("inf"), True):
            with self.subTest(percent=percent):
                self.usage(self.window(percent))
                self.assertFalse(self.evaluate().allowed)
        self.usage(UsageWindow("tokens", 90, self.now, unit="tokens"))
        self.assertEqual(self.evaluate().reasons, ("usage_invalid",))

    def test_adapter_failure_is_redacted_and_blocks(self):
        class FailingUsage:
            def read_usage(self, account, now):
                raise RuntimeError("provider-secret")

        self.config["global"]["reserve_enabled"] = True
        self.gates.adapters[("fake", 1)] = FailingUsage()
        decision = self.evaluate()
        self.assertEqual(decision.reasons, ("usage_unavailable",))
        self.assertNotIn("provider-secret", str(decision.data()))

    def test_duplicate_allowance_ids_block(self):
        self.usage(self.window(80), self.window(80))
        self.assertEqual(self.evaluate().reasons, ("usage_invalid",))

    def test_reported_reset_requires_new_observation(self):
        self.usage(self.window(reset=self.now))
        self.assertEqual(self.evaluate().reasons, ("usage_stale",))

    def test_provider_reset_resumes_retained_claim(self):
        self.claim()
        self.usage(self.window(40, reset=self.now + timedelta(minutes=1)))
        self.assertFalse(self.evaluate(checkpoint="periodic", run_id="run").allowed)
        self.now += timedelta(minutes=1)
        self.usage(self.window(100, reset=self.now + timedelta(days=1)))
        self.assertTrue(self.evaluate(checkpoint="periodic", run_id="run").allowed)
        self.assertEqual(sum(self.state["gates"]["counts"].values()), 1)

    def test_gate_evidence_contains_reading_and_reset_freshness(self):
        reset = self.now + timedelta(days=1)
        self.usage(self.window(reset=reset))
        decision = self.evaluate()
        self.assertEqual(
            decision.readings[0]["windows"][0]["reset_at"], reset.isoformat()
        )
        self.assertEqual(self.journal.load()["gates"]["checkpoint"], decision.data())

    def test_remote_claim_without_original_accounting_blocks_resume(self):
        self.state["remote_active_run_id"] = "remote"
        decision = self.evaluate(checkpoint="stage", run_id="remote")
        self.assertEqual(decision.reasons, ("claim_accounting_unknown",))

    def test_pending_claim_blocks_evaluator_until_reconciled(self):
        decision = self.evaluate()
        self.gates.prepare_claim(self.state, "run", decision)
        self.assertIn("reconciliation_required", self.evaluate().reasons)

    def test_expired_claim_checkpoint_cannot_prepare_a_claim(self):
        decision = self.evaluate()
        self.now += timedelta(seconds=60)
        with self.assertRaises(JournalError):
            self.gates.prepare_claim(self.state, "run", decision)

    def test_superseded_claim_checkpoint_cannot_prepare_a_claim(self):
        decision = self.evaluate()
        self.config["global"]["paused"] = True
        self.evaluate()
        with self.assertRaises(JournalError):
            self.gates.prepare_claim(self.state, "run", decision)

    def test_frozen_account_cannot_disappear_during_existing_run(self):
        self.usage(self.window(80))
        self.claim()
        self.config["accounts"] = [{"backend": "other", "credential_id": 2}]
        self.gates.adapters[("other", 2)] = FakeUsage([self.window(80)])
        self.gates.adapters[("fake", 1)] = FakeUsage([self.window(40)])
        decision = self.evaluate(checkpoint="model", run_id="run")
        self.assertEqual(decision.reasons, ("usage_reserve",))
        self.assertEqual(len(decision.readings), 2)

    def test_usage_expiry_shortens_next_check(self):
        self.usage(self.window(observed=self.now - timedelta(seconds=299)))
        decision = self.evaluate()
        self.assertEqual(
            decision.next_check_at, (self.now + timedelta(seconds=1)).isoformat()
        )

    def test_usage_reset_shortens_next_check(self):
        self.usage(self.window(reset=self.now + timedelta(seconds=2)))
        self.assertEqual(
            self.evaluate().next_check_at, (self.now + timedelta(seconds=2)).isoformat()
        )

    def test_outside_window_is_retained_wait_not_a_finish(self):
        self.claim()
        self.config["global"].update(
            schedule_mode="windows",
            windows=[{"days": [0], "start": "22:00", "end": "23:30"}],
        )
        self.now += timedelta(minutes=30)
        decision = self.evaluate(checkpoint="stage", run_id="run")
        self.assertEqual(decision.reasons, ("outside_window",))
        self.assertEqual(self.state["gates"]["owned_run_id"], "run")
        self.now += timedelta(days=7)
        self.now -= timedelta(minutes=1)
        self.assertTrue(self.evaluate(checkpoint="stage", run_id="run").allowed)
        self.assertEqual(sum(self.state["gates"]["counts"].values()), 1)

    def test_overlapping_windows_do_not_reset_count_at_second_start(self):
        self.config["global"].update(
            schedule_mode="windows",
            windows=[
                {"days": [0], "start": "22:00", "end": "01:00"},
                {"days": [1], "start": "00:00", "end": "03:00"},
            ],
        )
        self.claim()
        self.gates.complete_run(self.state, "run")
        self.now += timedelta(hours=2)
        self.assertEqual(self.evaluate().reasons, ("window_limit",))


class ScheduleTests(TestCase):
    def setUp(self):
        self.controls = copy.deepcopy(CONFIG["global"])
        self.controls.update(
            schedule_mode="windows",
            windows=[{"days": [0], "start": "22:00", "end": "06:00"}],
        )

    def at(self, text):
        return schedule(self.controls, datetime.fromisoformat(text))

    def test_overnight_uses_starting_weekday(self):
        monday, _ = self.at("2026-11-16T23:00:00+00:00")
        tuesday, _ = self.at("2026-11-17T05:59:00+00:00")
        self.assertEqual(monday, tuesday)
        self.assertIsNone(self.at("2026-11-18T05:00:00+00:00")[0])

    def test_open_inclusive_close_exclusive(self):
        self.assertIsNone(self.at("2026-11-16T21:59:59+00:00")[0])
        self.assertIsNotNone(self.at("2026-11-16T22:00:00+00:00")[0])
        interval, next_open = self.at("2026-11-17T06:00:00+00:00")
        self.assertIsNone(interval)
        self.assertEqual(next_open, datetime(2026, 11, 23, 22, tzinfo=UTC))

    def test_overlapping_and_touching_windows_have_one_stable_identity(self):
        self.controls["windows"].extend(
            [
                {"days": [1], "start": "05:00", "end": "08:00"},
                {"days": [1], "start": "08:00", "end": "09:00"},
            ]
        )
        before, _ = self.at("2026-11-16T23:00:00+00:00")
        after, _ = self.at("2026-11-17T08:30:00+00:00")
        self.assertEqual(before.id, after.id)
        self.assertEqual(after.end, datetime(2026, 11, 17, 9, tzinfo=UTC))
        self.controls["windows"].reverse()
        self.assertEqual(self.at("2026-11-17T08:30:00+00:00")[0].id, after.id)

    def test_week_boundary_overlap_identity_is_stable(self):
        self.controls["windows"] = [
            {"days": [6], "start": "22:00", "end": "03:00"},
            {"days": [0], "start": "01:00", "end": "07:00"},
        ]
        before, _ = self.at("2026-11-15T23:00:00+00:00")
        after, _ = self.at("2026-11-16T06:00:00+00:00")
        self.assertEqual(before.id, after.id)

    def test_dst_nonexistent_open_shifts_to_first_valid_instant(self):
        self.controls.update(
            timezone="America/Los_Angeles",
            windows=[{"days": [6], "start": "02:30", "end": "04:00"}],
        )
        self.assertIsNone(self.at("2026-03-08T09:59:59+00:00")[0])
        interval, _ = self.at("2026-03-08T10:00:00+00:00")
        self.assertEqual(interval.start, datetime(2026, 3, 8, 10, tzinfo=UTC))

    def test_dst_nonexistent_close_shifts_to_first_valid_instant(self):
        self.controls.update(
            timezone="America/Los_Angeles",
            windows=[{"days": [6], "start": "01:00", "end": "02:30"}],
        )
        interval, _ = self.at("2026-03-08T09:30:00+00:00")
        self.assertEqual(interval.end, datetime(2026, 3, 8, 10, tzinfo=UTC))
        self.assertIsNone(self.at("2026-03-08T10:00:00+00:00")[0])

    def test_dst_repeated_open_uses_earlier_fold_and_close_later_fold(self):
        self.controls.update(
            timezone="America/Los_Angeles",
            windows=[{"days": [6], "start": "01:15", "end": "01:45"}],
        )
        first, _ = self.at("2026-11-01T08:20:00+00:00")
        second, _ = self.at("2026-11-01T09:40:00+00:00")
        self.assertEqual(first.id, second.id)
        self.assertEqual(first.start, datetime(2026, 11, 1, 8, 15, tzinfo=UTC))
        self.assertEqual(first.end, datetime(2026, 11, 1, 9, 45, tzinfo=UTC))
        self.assertIsNone(self.at("2026-11-01T09:45:00+00:00")[0])

    def test_continuous_day_is_23_hours_on_spring_dst(self):
        self.controls.update(schedule_mode="continuous", timezone="America/Los_Angeles")
        interval, _ = self.at("2026-03-08T12:00:00+00:00")
        self.assertEqual(interval.end - interval.start, timedelta(hours=23))

    def test_continuous_day_is_25_hours_on_fall_dst(self):
        self.controls.update(schedule_mode="continuous", timezone="America/Los_Angeles")
        interval, _ = self.at("2026-11-01T12:00:00+00:00")
        self.assertEqual(interval.end - interval.start, timedelta(hours=25))

    def test_fully_covered_week_uses_local_days(self):
        self.controls["windows"] = [
            {"days": list(range(7)), "start": "00:00", "end": "12:00"},
            {"days": list(range(7)), "start": "12:00", "end": "00:00"},
        ]
        interval, _ = self.at("2026-11-16T23:00:00+00:00")
        self.assertEqual(interval.id, "day:UTC:2026-11-16")

    def test_no_configured_windows_is_closed(self):
        self.controls["windows"] = []
        self.assertEqual(self.at("2026-11-16T23:00:00+00:00"), (None, None))


class ControllerGateTests(ServerCase):
    def test_poll_persists_and_publishes_gates_without_claiming(self):
        config = copy.deepcopy(CONFIG)
        config["execution_ready"] = False
        config["global"]["reserve_enabled"] = True
        self.server.responses["/api/v1/runner/configuration/"] = (200, config)
        self.server.responses["/api/v1/runner/gates/report/"] = (
            200,
            {"schema_version": 1},
        )
        stop = threading.Event()
        with Journal(self.path) as journal:
            with patch.object(stop, "wait", side_effect=lambda delay: stop.set()):
                Controller(self.client, journal, stop).run()
            report = journal.load()["gates"]["checkpoint"]
        self.assertIn("release_disabled", report["reasons"])
        self.assertIn("usage_missing", report["reasons"])
        writes = [path for method, path in self.server.calls if method == "POST"]
        self.assertEqual(writes, ["/api/v1/runner/gates/report/"])


if __name__ == "__main__":
    main()
