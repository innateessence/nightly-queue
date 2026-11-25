import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from .compact_role_qualification import PROBE


class CurrentPermissionProbeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "probe.txt"
        self.source.write_bytes(b"candidate input\n")
        self.private = self.root / "private"
        self.private.write_bytes(b"protected")
        self.private.chmod(0)

    def probe(self, role):
        context = self.root / "context.json"
        context.write_text(json.dumps({"private_path": str(self.private)}))
        # Exercise permission checks without the qualification-only descendant launch.
        script = PROBE.partition("subprocess.Popen(")[0]
        script = script.replace("/work/probe.txt", str(self.source))
        script = script.replace("/work/new-file", str(self.root / "new-file"))
        script = script.replace("1000", str(os.getuid()))
        return subprocess.run(
            [sys.executable, "-I", "-c", script],
            env={"VIBEARR_CONTEXT": str(context), "VIBEARR_FACTORY_ROLE": role},
            capture_output=True,
            timeout=5,
        )

    def test_build_probe_observes_writable_source_and_denied_private_file(self):
        result = self.probe("build")
        self.assertEqual(result.returncode, 0, result.stderr.decode())

    def test_task_owned_read_only_source_cannot_qualify_an_immutable_role(self):
        self.source.chmod(0o444)
        result = self.probe("reviewer-code")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b"AssertionError", result.stderr)

    def test_missing_private_sentinel_cannot_be_reported_as_denied_access(self):
        self.private.unlink()
        result = self.probe("build")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b"FileNotFoundError", result.stderr)
