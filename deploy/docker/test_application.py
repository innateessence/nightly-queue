from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from django.db import connection
from django.test import TransactionTestCase

from .application import (
    STARTUP_LOCK,
    database_startup_lock,
    group_running,
    startup_command,
    stop_processes,
    supervise,
)


class ApplicationProcessTests(unittest.TestCase):
    def command(self, source):
        return [sys.executable, "-c", source]

    def test_successful_startup_command_returns_true(self):
        self.assertTrue(startup_command(self.command("pass"), threading.Event()))

    def test_failed_startup_command_preserves_failure(self):
        with self.assertRaises(subprocess.CalledProcessError) as error:
            startup_command(self.command("raise SystemExit(7)"), threading.Event())
        self.assertEqual(error.exception.returncode, 7)

    def test_canceled_startup_does_not_launch_a_command(self):
        stop = threading.Event()
        stop.set()
        with patch("deploy.docker.application.subprocess.Popen") as launch:
            self.assertFalse(startup_command(self.command("pass"), stop))
        launch.assert_not_called()

    def test_timed_out_startup_command_is_terminated(self):
        command = self.command("import time; time.sleep(60)")
        launched = []
        original = subprocess.Popen

        def launch(*args, **kwargs):
            process = original(*args, **kwargs)
            launched.append(process)
            return process

        with patch("deploy.docker.application.subprocess.Popen", side_effect=launch):
            with self.assertRaisesRegex(RuntimeError, "timed out"):
                startup_command(command, threading.Event(), timeout=0)
        self.assertFalse(group_running(launched[0]))

    def test_unexpected_successful_child_exit_fails_supervisor(self):
        commands = [self.command("pass"), self.command("import time; time.sleep(60)")]
        self.assertEqual(supervise(commands, threading.Event()), 1)

    def test_stop_before_supervision_does_not_start_children(self):
        stop = threading.Event()
        stop.set()
        with patch("deploy.docker.application.subprocess.Popen") as launch:
            self.assertEqual(supervise([self.command("pass")], stop), 0)
        launch.assert_not_called()

    def test_hung_child_is_killed_within_shutdown_bound(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "ready"
            source = "import signal,time; from pathlib import Path; "
            source += "signal.signal(signal.SIGTERM,signal.SIG_IGN); "
            source += f"Path({str(marker)!r}).write_text('ready'); time.sleep(60)"
            process = subprocess.Popen(self.command(source), start_new_session=True)
            self.addCleanup(stop_processes, [process], 0.1)
            deadline = time.monotonic() + 3
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(marker.exists())
            started = time.monotonic()
            stop_processes([process], timeout=0.1)
            self.assertEqual(process.returncode, -signal.SIGKILL)
            self.assertLess(time.monotonic() - started, 3)

    def test_stop_during_supervision_terminates_running_children(self):
        stop = threading.Event()
        timer = threading.Timer(0.2, stop.set)
        self.addCleanup(timer.cancel)
        timer.start()
        started = time.monotonic()
        command = self.command("import time; time.sleep(60)")
        self.assertEqual(supervise([command], stop, shutdown_timeout=0.1), 0)
        self.assertLess(time.monotonic() - started, 3)

    def test_failed_second_launch_terminates_first_child(self):
        launched = []
        original = subprocess.Popen

        def launch(*args, **kwargs):
            if launched:
                raise FileNotFoundError("second executable is missing")
            process = original(*args, **kwargs)
            launched.append(process)
            return process

        with patch("deploy.docker.application.subprocess.Popen", side_effect=launch):
            with self.assertRaises(FileNotFoundError):
                supervise(
                    [self.command("import time; time.sleep(60)")] * 2, threading.Event()
                )
        self.assertFalse(group_running(launched[0]))

    def test_exited_leader_does_not_leave_a_surviving_process_group(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "ready"
            child = "import signal,time; from pathlib import Path; "
            child += "signal.signal(signal.SIGTERM,signal.SIG_IGN); "
            child += f"Path({str(marker)!r}).write_text('ready'); time.sleep(60)"
            parent = f"import subprocess,sys; subprocess.Popen([sys.executable,'-c',{child!r}])"
            process = subprocess.Popen(self.command(parent), start_new_session=True)
            self.addCleanup(stop_processes, [process], 0.1)
            deadline = time.monotonic() + 3
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(marker.exists())
            self.assertEqual(process.wait(timeout=3), 0)
            self.assertTrue(group_running(process))
            stop_processes([process], timeout=0.1)
            self.assertFalse(group_running(process))

    def test_disappearing_process_group_is_harmless(self):
        process = subprocess.Popen(self.command("pass"), start_new_session=True)
        process.wait(timeout=3)
        stop_processes([process], timeout=0.1)
        self.assertEqual(process.returncode, 0)


class DatabaseStartupLockTests(TransactionTestCase):
    def contender(self):
        contender = connection.copy(alias="startup-test")
        self.addCleanup(contender.close)
        return contender

    def can_acquire(self, contender):
        with contender.cursor() as cursor:
            cursor.execute("SELECT pg_try_advisory_lock(%s)", [STARTUP_LOCK])
            acquired = cursor.fetchone()[0]
            if acquired:
                cursor.execute("SELECT pg_advisory_unlock(%s)", [STARTUP_LOCK])
        return acquired

    def test_duplicate_app_start_cannot_enter_migrations(self):
        first, second = self.contender(), self.contender()
        with database_startup_lock(first, threading.Event()) as acquired:
            self.assertTrue(acquired)
            self.assertFalse(self.can_acquire(second))
        self.assertTrue(self.can_acquire(second))
        self.assertIsNone(first.connection)

    def test_migration_failure_releases_startup_lock(self):
        first, second = self.contender(), self.contender()
        with self.assertRaisesRegex(RuntimeError, "migration failed"):
            with database_startup_lock(first, threading.Event()):
                raise RuntimeError("migration failed")
        self.assertTrue(self.can_acquire(second))
        self.assertIsNone(first.connection)

    def test_waiting_startup_has_a_bounded_deadline(self):
        first, second = self.contender(), self.contender()
        with database_startup_lock(first, threading.Event()):
            with self.assertRaisesRegex(RuntimeError, "lock timed out"):
                with database_startup_lock(second, threading.Event(), timeout=0):
                    self.fail("Contending startup must not enter migration")
        self.assertIsNone(second.connection)

    def test_canceled_startup_does_not_acquire_a_lock(self):
        contender = self.contender()
        stop = threading.Event()
        stop.set()
        with database_startup_lock(contender, stop) as acquired:
            self.assertFalse(acquired)
        self.assertIsNone(contender.connection)
