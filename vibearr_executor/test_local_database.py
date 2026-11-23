import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from .local_database import BIN, UID, LocalDatabase, LocalDatabaseError


class LocalDatabaseTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.database = LocalDatabase(self.root)

    def test_command_drops_to_the_private_database_identity_without_inherited_auth(
        self,
    ):
        with patch("vibearr_executor.local_database.subprocess.Popen") as run:
            run.return_value.communicate.return_value = (b"", b"")
            run.return_value.returncode = 0
            self.database.command("postgres", "--version")
        options = run.call_args.kwargs
        self.assertEqual(options["user"], UID)
        self.assertEqual(options["group"], UID)
        self.assertEqual(options["extra_groups"], [])
        self.assertNotIn("PGPASSWORD", options["env"])

    def test_sql_credentials_never_enter_process_arguments(self):
        with (
            patch(
                "vibearr_executor.local_database.pwd.getpwuid",
                return_value=SimpleNamespace(pw_name="dbadmin"),
            ),
            patch.object(self.database, "command") as command,
        ):
            self.database.sql("SELECT 'private-test-value';")
        self.assertNotIn("private-test-value", " ".join(command.call_args.args))
        self.assertEqual(
            command.call_args.kwargs["input"], b"SELECT 'private-test-value';"
        )

    def test_command_timeout_terminates_the_owned_process_group(self):
        timeout = subprocess.TimeoutExpired("initdb", 0.1)
        with (
            patch("vibearr_executor.local_database.subprocess.Popen") as spawn,
            patch("vibearr_executor.local_database.os.killpg") as kill,
        ):
            spawn.return_value.pid = 123
            spawn.return_value.communicate.side_effect = [timeout, (b"", b"")]
            with self.assertRaises(subprocess.TimeoutExpired):
                self.database.command("initdb", timeout=0.1)
        kill.assert_called_once_with(123, 9)

    def test_provenance_rejects_a_different_installed_major(self):
        result = subprocess.CompletedProcess(
            [], 0, b"postgres (PostgreSQL) 17.1\n", b""
        )
        with patch.object(self.database, "command", return_value=result):
            with self.assertRaisesRegex(LocalDatabaseError, "version differs"):
                self.database.provenance()

    def test_createdb_must_be_an_explicit_boolean_from_a_trusted_profile(self):
        with self.assertRaisesRegex(ValueError, "trusted profile"):
            self.database.prepare("operation", allow_createdb="yes")

    def test_reset_cleans_stale_state_even_when_started_is_false(self):
        (self.root / "old-phase").write_text("old")
        self.database.reset("operation")
        self.assertEqual(list(self.root.iterdir()), [])

    def test_invalid_pid_state_fences_and_preserves_storage(self):
        self.database.data.mkdir()
        (self.database.data / "postmaster.pid").write_text("not-a-pid\n")
        with self.assertRaisesRegex(LocalDatabaseError, "PID state is invalid"):
            self.database.reset("operation")
        self.assertTrue(self.database.fenced)
        self.assertTrue((self.database.data / "postmaster.pid").exists())

    def test_reused_pid_is_never_signaled_or_deleted(self):
        process = subprocess.Popen(
            ["/usr/local/bin/python", "-c", "import time; time.sleep(30)"]
        )
        self.addCleanup(process.wait, timeout=3)
        self.addCleanup(process.terminate)
        self.database.data.mkdir()
        (self.database.data / "postmaster.pid").write_text(str(process.pid) + "\n")
        with patch("vibearr_executor.local_database.os.kill") as kill:
            with self.assertRaisesRegex(LocalDatabaseError, "identity is uncertain"):
                self.database.reset("operation")
        kill.assert_not_called()
        self.assertTrue(self.database.fenced)
        self.assertTrue(self.database.data.exists())

    def test_database_paths_must_be_absolute_trusted_configuration(self):
        with self.assertRaisesRegex(ValueError, "trusted absolute"):
            LocalDatabase(Path("relative"))


@unittest.skipUnless(
    os.environ.get("VIBEARR_LOCAL_DB_ACCEPTANCE") == "1",
    "explicit Docker local-database probe",
)
class LocalDatabaseAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.assertEqual(
            os.getuid(),
            0,
            "Local database probes require trusted Docker PID 1 privileges.",
        )
        self.assertTrue(
            (BIN / "postgres").is_file(),
            "Install the declared PostgreSQL binaries in the probe image.",
        )
        temporary = tempfile.TemporaryDirectory(prefix="local-db-probe-")
        self.addCleanup(temporary.cleanup)
        parent = Path(temporary.name)
        parent.chmod(0o711)
        self.root = parent / "database"
        self.database = LocalDatabase(self.root)
        self.addCleanup(self.database.reset, "cleanup")

    def task(self, source, url):
        result = subprocess.run(
            ["/usr/local/bin/python", "-c", source],
            input=url.encode(),
            user=1000,
            group=1000,
            extra_groups=[],
            cwd="/",
            env={"PATH": "/usr/local/bin:/usr/bin:/bin"},
            check=False,
            capture_output=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        return result.stdout.decode().strip()

    def query(self, url, statement):
        source = "import sys,psycopg2; c=psycopg2.connect(sys.stdin.read()); c.autocommit=True; q=c.cursor(); "
        source += "q.execute(" + repr(statement) + "); print(q.fetchall())"
        return self.task(source, url)

    def test_task_is_not_an_administrator_and_cannot_escalate(self):
        url = self.database.prepare("privileges")
        flags = self.query(
            url,
            "SELECT rolsuper,rolcreaterole,rolreplication,rolbypassrls,rolinherit,rolconnlimit FROM pg_roles WHERE rolname=current_user",
        )
        self.assertEqual(flags, "[(False, False, False, False, True, 32)]")
        code = "import sys,psycopg2; c=psycopg2.connect(sys.stdin.read()); c.autocommit=True; q=c.cursor(); "
        code += "\ntry: q.execute('ALTER ROLE task SUPERUSER')\nexcept psycopg2.errors.InsufficientPrivilege: print('denied')\nelse: raise RuntimeError('task escalated')"
        self.assertEqual(self.task(code, url), "denied")

    def test_task_cannot_read_admin_storage_or_use_its_private_socket(self):
        url = self.database.prepare("private-admin")
        source = "import sys,pathlib,psycopg2; sys.stdin.read(); "
        source += (
            "\ntry: list(pathlib.Path("
            + repr(str(self.root))
            + ").iterdir())\nexcept PermissionError: print('storage denied')\nelse: raise RuntimeError('admin storage readable')"
        )
        source += (
            "\ntry: psycopg2.connect(host="
            + repr(str(self.root))
            + ", user='testdatabase', dbname='postgres', connect_timeout=2)\nexcept psycopg2.OperationalError: print('socket denied')\nelse: raise RuntimeError('admin socket usable')"
        )
        self.assertEqual(self.task(source, url), "storage denied\nsocket denied")

    def test_django_profile_can_create_and_drop_its_test_database(self):
        url = self.database.prepare("django", allow_createdb=True)
        source = "import sys,psycopg2; c=psycopg2.connect(sys.stdin.read()); c.autocommit=True; q=c.cursor(); q.execute('CREATE DATABASE test_task'); q.execute('DROP DATABASE test_task'); print('created and dropped')"
        self.assertEqual(self.task(source, url), "created and dropped")

    def test_profile_can_deny_database_creation(self):
        url = self.database.prepare("no-createdb", allow_createdb=False)
        source = "import sys,psycopg2; c=psycopg2.connect(sys.stdin.read()); c.autocommit=True; q=c.cursor(); "
        source += "\ntry: q.execute('CREATE DATABASE extra')\nexcept psycopg2.errors.InsufficientPrivilege: print('denied')\nelse: raise RuntimeError('createdb granted')"
        self.assertEqual(self.task(source, url), "denied")

    def test_next_phase_has_fresh_credentials_data_and_role_membership(self):
        first = self.database.prepare("first")
        source = "import sys,psycopg2; c=psycopg2.connect(sys.stdin.read()); c.autocommit=True; q=c.cursor(); q.execute('CREATE TABLE phase_secret(value text)'); q.execute(\"INSERT INTO phase_secret VALUES ('first')\"); print('saved')"
        self.assertEqual(self.task(source, first), "saved")
        second = self.database.prepare("second")
        self.assertNotEqual(first, second)
        self.assertEqual(
            self.query(second, "SELECT to_regclass('public.phase_secret')"), "[(None,)]"
        )
        self.assertEqual(
            self.query(
                second,
                "SELECT count(*) FROM pg_auth_members WHERE member=(SELECT oid FROM pg_roles WHERE rolname='task')",
            ),
            "[(0,)]",
        )

    def test_a_new_manager_stops_an_existing_postmaster_without_a_started_flag(self):
        self.database.prepare("restart")
        process = self.database.process
        replacement = LocalDatabase(self.root)
        replacement.reset("reconcile")
        process.wait(timeout=1)
        self.assertEqual(list(self.root.iterdir()), [])
        self.database.process = None

    def test_postmaster_crash_does_not_leave_a_backend_after_reset(self):
        url = self.database.prepare("postmaster-crash")
        source = (
            "import sys,psycopg2; c=psycopg2.connect(sys.stdin.read()); q=c.cursor(); "
        )
        source += (
            "q.execute('SELECT pg_backend_pid()'); print(q.fetchone()[0],flush=True); "
        )
        source += "q.execute('SELECT pg_sleep(30)')"
        task = subprocess.Popen(
            [sys.executable, "-c", source],
            user=1000,
            group=1000,
            extra_groups=[],
            cwd="/",
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.addCleanup(task.stdout.close)
        self.addCleanup(task.stderr.close)
        self.addCleanup(task.wait, timeout=3)
        self.addCleanup(task.kill)
        task.stdin.write(url.encode())
        task.stdin.close()
        backend = int(task.stdout.readline())
        os.kill(self.database.process.pid, signal.SIGKILL)
        self.database.process.wait(timeout=3)
        self.database.reset("after-crash")
        task.wait(timeout=3)
        self.assertFalse(self.database.process_identity(backend))
        self.assertEqual(list(self.root.iterdir()), [])

    def test_role_creation_failure_stops_the_started_database_before_reuse(self):
        with patch.object(
            self.database, "sql", side_effect=RuntimeError("role setup failed")
        ):
            with self.assertRaisesRegex(RuntimeError, "role setup failed"):
                self.database.prepare("partial-start")
        self.assertIsNone(self.database.process)
        self.assertFalse(self.database.fenced)
        self.assertEqual(list(self.root.iterdir()), [])
        self.assertTrue(self.database.prepare("retry").startswith("postgresql://task:"))

    def test_initdb_timeout_cleans_its_process_group_before_reuse(self):
        original = self.database.command

        def timed_command(*arguments, **kwargs):
            if arguments[0] == "initdb":
                kwargs["timeout"] = 0.00001
            return original(*arguments, **kwargs)

        with patch.object(self.database, "command", side_effect=timed_command):
            with self.assertRaises(subprocess.TimeoutExpired):
                self.database.prepare("timed-initdb")
        self.assertIsNone(self.database.process)
        self.assertEqual(list(self.root.iterdir()), [])
        self.assertTrue(self.database.prepare("retry").startswith("postgresql://task:"))

    def test_canceled_startup_stops_the_postmaster(self):
        cancel = threading.Event()
        cancel.set()
        with self.assertRaisesRegex(LocalDatabaseError, "canceled"):
            self.database.prepare("canceled", cancel=cancel)
        self.assertIsNone(self.database.process)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_failed_stop_fences_storage_until_actual_termination_is_confirmed(self):
        self.database.prepare("stop-failure")
        with patch.object(
            self.database,
            "command",
            side_effect=subprocess.TimeoutExpired("pg_ctl", 15),
        ):
            with self.assertRaises(subprocess.TimeoutExpired):
                self.database.reset("stop")
        self.assertTrue(self.database.fenced)
        self.assertTrue(self.database.data.exists())
        self.database.reset("reconcile")
        self.assertFalse(self.database.fenced)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_provenance_reports_the_actual_binary_not_a_database_image(self):
        provenance = self.database.provenance()
        self.assertEqual(provenance["postgresql_major"], 15)
        self.assertEqual(len(provenance["postgres_sha256"]), 64)
        self.assertNotIn("image", provenance)
        self.assertEqual(provenance, json.loads(json.dumps(provenance)))
