import os
from pathlib import Path
import subprocess
import tempfile
import unittest


FAKE_DOCKER = """#!/bin/bash
printf '%s\\n' "$*" >> "$CALL_LOG"
case "$*" in
  'build '*) exit 0 ;;
  'compose config --format json') echo '{}' ;;
  'run --rm -i '*)
    cat >/dev/null
    printf '%s\\n' review db user database image runner egress application-secrets database-secrets runner-secrets runner-data runner-logins worker-secrets worker-control postgres-data
    ;;
  'compose ps '*) exit 77 ;;
  *) exit 78 ;;
esac
"""


class OperationsInspectionFailureTests(unittest.TestCase):
    def assert_inspection_failure_stops_action(self, action, identity):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            docker = root / "docker"
            docker.write_text(FAKE_DOCKER)
            docker.chmod(0o700)
            log = root / "calls"
            environment = dict(os.environ, PATH=str(root) + ":" + os.environ["PATH"])
            environment["CALL_LOG"] = str(log)
            script = Path(__file__).with_name("operations.sh")
            result = subprocess.run(
                ["bash", str(script), action, identity],
                env=environment,
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(result.returncode, 77, result.stderr)
            calls = log.read_text().splitlines()
            self.assertTrue(calls[-1].startswith("compose ps "), calls)

    def test_backup_stops_if_container_inspection_fails(self):
        self.assert_inspection_failure_stops_action("backup", "--quiesced")

    def test_restore_files_stops_if_container_inspection_fails(self):
        self.assert_inspection_failure_stops_action(
            "restore-files", "01234567-89ab-4cde-8f01-23456789abcd"
        )

    def test_restore_bootstrap_stops_if_worker_inspection_fails(self):
        self.assert_inspection_failure_stops_action(
            "restore-bootstrap", "01234567-89ab-4cde-8f01-23456789abcd"
        )
