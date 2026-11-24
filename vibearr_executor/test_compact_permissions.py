from pathlib import Path
import os
import subprocess
import tempfile
import unittest

from .compact_profiles import profiles
from .compact_worker import CompactWorkerRuntime


@unittest.skipUnless(
    os.getuid() == 0, "Run the explicit Docker UID-boundary probe as root."
)
class CompactPermissionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.root.chmod(0o755)
        self.work = self.root / "source"
        self.work.mkdir(mode=0o700)
        os.chown(self.work, 1000, 1000)
        self.file = self.work / "candidate.py"
        self.file.write_text("original source\n")
        self.file.chmod(0o644)
        os.chown(self.file, 1000, 1000)
        self.secret = self.root / "controller-secret"
        self.secret.write_text("protected synthetic sentinel")
        self.secret.chmod(0o600)
        installed, _validation = profiles("sha256:" + "a" * 64, "b" * 64)
        self.profiles = {profile.profile_id: profile for profile in installed}
        self.runtime = object.__new__(CompactWorkerRuntime)
        self.runtime.work = self.work
        self.runtime.source_modes = {}

    def probe(self):
        code = """import json,os,pathlib,sys
source=pathlib.Path(sys.argv[1]);secret=pathlib.Path(sys.argv[2]);value={}
for key,action in [('edit',lambda:source.open('r+b')),('create',lambda:(source.parent/'new-file').open('w')),('chmod',lambda:source.chmod(0o644)),('control',lambda:secret.open('rb'))]:
    try:
        result=action()
        if hasattr(result,'close'):result.close()
        value[key]=True
    except PermissionError:value[key]=False
print(json.dumps(value))
"""

        def identity():
            os.setgroups([])
            os.setgid(1000)
            os.setuid(1000)

        result = subprocess.run(
            [
                "/usr/local/bin/python",
                "-I",
                "-c",
                code,
                str(self.file),
                str(self.secret),
            ],
            preexec_fn=identity,
            check=True,
            capture_output=True,
            text=True,
        )
        import json

        return json.loads(result.stdout)

    def test_read_only_role_denies_file_edit_creation_and_chmod(self):
        profile = self.profiles["factory-reviewer-code"]
        self.runtime.prepare_source(profile, {})
        self.assertEqual(
            self.probe(),
            {"edit": False, "create": False, "chmod": False, "control": False},
        )
        self.runtime.finish_source(profile, {})
        self.assertEqual(self.file.stat().st_mode & 0o777, 0o644)
        self.assertEqual(self.file.read_text(), "original source\n")

    def test_builder_can_edit_candidate_but_cannot_read_controller_secret(self):
        self.runtime.prepare_source(self.profiles["factory-build"], {})
        self.assertEqual(
            self.probe(),
            {"edit": True, "create": True, "chmod": True, "control": False},
        )

    def test_browser_candidate_is_read_only_for_the_disposable_app_uid(self):
        from dataclasses import replace

        profile = replace(
            self.profiles["factory-reviewer-ui"],
            profile_id="browser-ui",
            phase="validation",
        )
        self.runtime.prepare_source(profile, {})
        self.assertEqual(self.file.stat().st_uid, 0)
        self.assertEqual(self.work.stat().st_mode & 0o777, 0o555)
        self.assertEqual(self.file.stat().st_mode & 0o777, 0o444)
