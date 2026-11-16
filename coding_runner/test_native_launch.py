from dataclasses import replace
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from coding_runner.test_native_delivery import Host, GitHub
from vibearr_runner import processes
from vibearr_runner.backends import BackendInterrupted
from vibearr_runner.budgets import BudgetExceeded
from vibearr_runner.client import APIError
from vibearr_runner.delivery import DeliveryError, SupervisorGit
from vibearr_runner.journal import Journal
from vibearr_runner.native_delivery import NativeDelivery
from vibearr_runner.recovery import ReconciliationRequired


class NativeMutationLaunchTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.host, self.github = Host(), GitHub()
        self.host.workspace_value = replace(
            self.host.workspace_value, directory=self.directory
        )
        self.host.workspace_value.checkout.mkdir()
        self.marker = self.directory / "mutation"
        self.journal = Journal(self.directory / "journal")
        self.journal.__enter__()
        self.addCleanup(self.journal.__exit__)
        self.host.state.update(self.journal.load())
        self.host.state["native"] = self.host.native
        self.host.save = lambda: self.journal.save(self.host.state)
        self.tracker = processes.ProcessTracker(self.journal, self.host.state)
        token = processes.current_tracker.set(self.tracker)
        self.addCleanup(processes.current_tracker.reset, token)
        self.transport = SupervisorGit(token="fixture-token")
        clients = patch.object(
            NativeDelivery, "clients", return_value=(self.github, self.transport)
        )
        clients.start()
        self.addCleanup(clients.stop)
        verify = patch.object(NativeDelivery, "verify")
        verify.start()
        self.addCleanup(verify.stop)
        self.remote = patch.object(
            self.transport, "observed_remote", side_effect=self.observed
        )
        self.remote.start()
        self.addCleanup(self.remote.stop)
        self.spawn = processes.popen

    def observed(self, workspace):
        return (
            self.host.native["candidate"]["workspace"]["commit_sha"]
            if self.marker.exists()
            else ""
        )

    def program(self, arguments, **kwargs):
        command = [
            sys.executable,
            "-c",
            "from pathlib import Path; Path(__import__('sys').argv[1]).write_text('mutation')",
            str(self.marker),
        ]
        return self.spawn(command, **kwargs)

    def execution(self, checkpoint=lambda: True, local=lambda: True, deadline=None):
        limit = deadline if deadline is not None else time.monotonic() + 30
        token = processes.current_execution.set((limit, local, checkpoint))
        self.addCleanup(processes.current_execution.reset, token)

    def assert_undispatched(self):
        self.assertFalse(self.host.native.get("delivery", {}).get("push_dispatched"))
        self.assertFalse(self.marker.exists())

    def test_pause_before_popen_preserves_undispatched_push(self):
        self.execution(checkpoint=lambda: False)
        with patch.object(
            processes, "popen", side_effect=AssertionError("unexpected spawn")
        ):
            with self.assertRaises(BackendInterrupted):
                NativeDelivery(self.host).step()
        self.assert_undispatched()

    def test_cancel_before_popen_preserves_undispatched_push(self):
        self.execution(local=lambda: False)
        with patch.object(
            processes, "popen", side_effect=AssertionError("unexpected spawn")
        ):
            with self.assertRaises(BackendInterrupted):
                NativeDelivery(self.host).step()
        self.assert_undispatched()

    def test_expired_deadline_before_popen_preserves_undispatched_push(self):
        self.execution(deadline=time.monotonic() - 1)
        with patch.object(
            processes, "popen", side_effect=AssertionError("unexpected spawn")
        ):
            with self.assertRaises(BudgetExceeded):
                NativeDelivery(self.host).step()
        self.assert_undispatched()

    def test_checkpoint_error_before_popen_preserves_undispatched_push(self):
        def unavailable():
            raise APIError("checkpoint unavailable")

        self.execution(checkpoint=unavailable)
        with patch.object(
            processes, "popen", side_effect=AssertionError("unexpected spawn")
        ):
            with self.assertRaises(APIError):
                NativeDelivery(self.host).step()
        self.assert_undispatched()

    def test_late_pause_reaps_blocked_worker_before_dispatch_then_resume_succeeds(self):
        decisions = iter([True, False])
        self.execution(checkpoint=lambda: next(decisions))
        with patch.object(processes, "popen", side_effect=self.program):
            with self.assertRaises(BackendInterrupted):
                NativeDelivery(self.host).step()
        self.assert_undispatched()
        self.assertTrue(
            all(p["status"] == "stopped" for p in self.host.state["processes"].values())
        )
        self.execution()
        with patch.object(processes, "popen", side_effect=self.program):
            self.assertEqual(NativeDelivery(self.host).step(), "continue")
        self.assertTrue(self.marker.exists())
        self.assertTrue(self.host.native["delivery"]["push_dispatched"])

    def test_deadline_expiring_inside_launch_gate_never_releases_mutation(self):
        clock = [100.0]
        self.execution(deadline=130.0)

        def delayed(arguments, **kwargs):
            clock[0] = 200.0
            return self.program(arguments, **kwargs)

        with patch.object(processes.time, "monotonic", side_effect=lambda: clock[0]):
            with patch.object(processes, "popen", side_effect=delayed):
                with self.assertRaises(BudgetExceeded):
                    NativeDelivery(self.host).step()
        self.assert_undispatched()

    def test_cancel_inside_launch_gate_reaps_worker_without_dispatch(self):
        allowed = iter([True, False])
        self.execution(local=lambda: next(allowed))
        with patch.object(processes, "popen", side_effect=self.program):
            with self.assertRaises(BackendInterrupted):
                NativeDelivery(self.host).step()
        self.assert_undispatched()
        self.assertTrue(
            all(p["status"] == "stopped" for p in self.host.state["processes"].values())
        )

    def test_checkpoint_error_inside_launch_gate_reaps_worker_without_dispatch(self):
        calls = []

        def checkpoint():
            calls.append(True)
            if len(calls) == 2:
                raise APIError("checkpoint unavailable")
            return True

        self.execution(checkpoint=checkpoint)
        with patch.object(processes, "popen", side_effect=self.program):
            with self.assertRaises(APIError):
                NativeDelivery(self.host).step()
        self.assert_undispatched()
        self.assertTrue(
            all(p["status"] == "stopped" for p in self.host.state["processes"].values())
        )

    def test_operating_system_rejecting_launch_is_a_known_undispatched_failure(self):
        self.execution()
        with patch.object(processes, "popen", side_effect=OSError("cannot spawn")):
            with self.assertRaises(DeliveryError) as caught:
                NativeDelivery(self.host).step()
        self.assertNotIsInstance(caught.exception, ReconciliationRequired)
        self.assert_undispatched()

    def test_dispatch_is_durable_before_actual_mutation_process_is_released(self):
        self.execution()
        original_save = self.host.save
        observed = []

        def save():
            if self.host.native.get("delivery", {}).get("push_dispatched"):
                observed.append(self.marker.exists())
            original_save()

        self.host.save = save
        with patch.object(processes, "popen", side_effect=self.program):
            self.assertEqual(NativeDelivery(self.host).step(), "continue")
        self.assertFalse(observed[0])
        self.assertTrue(self.journal.load()["native"]["delivery"]["push_dispatched"])
        self.assertTrue(self.marker.exists())

    def test_unknown_after_actual_launch_keeps_dispatch_and_never_repeats_mutation(
        self,
    ):
        self.execution()
        self.remote.stop()
        with patch.object(self.transport, "observed_remote", return_value=""):
            with patch.object(processes, "popen", side_effect=self.program) as spawned:
                with self.assertRaises(ReconciliationRequired):
                    NativeDelivery(self.host).step()
                with self.assertRaises(ReconciliationRequired):
                    NativeDelivery(self.host).step()
        self.assertEqual(spawned.call_count, 1)
        self.assertTrue(self.marker.exists())
        self.assertTrue(self.host.native["delivery"]["push_dispatched"])
