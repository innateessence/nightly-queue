import json
from pathlib import Path
import tempfile
import unittest

from deploy.docker.engineering_report import restored_boundary
from vibearr_runner.compact_evidence import digest


class RestoredBoundaryResponseTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "restored-boundary.json"
        self.report = {"kind": "actual-worker-boundary", "passed": True}
        self.wrapper = {"report": self.report, "sha256": digest(self.report)}

    def save(self):
        content = json.dumps(self.wrapper).encode()
        self.path.write_bytes(content)
        return content

    def test_authenticated_envelope_unwraps_and_preserves_raw_artifact(self):
        content = self.save()
        self.assertEqual(restored_boundary(self.path), self.report)
        self.assertEqual(self.path.read_bytes(), content)

    def test_changed_report_with_original_digest_is_rejected(self):
        self.report["passed"] = False
        self.save()
        with self.assertRaisesRegex(ValueError, "digest differs"):
            restored_boundary(self.path)

    def test_extra_envelope_fields_are_rejected(self):
        self.wrapper["passed"] = True
        self.save()
        with self.assertRaisesRegex(ValueError, "invalid envelope"):
            restored_boundary(self.path)

    def test_bare_report_is_rejected(self):
        self.path.write_text(json.dumps(self.report))
        with self.assertRaisesRegex(ValueError, "invalid envelope"):
            restored_boundary(self.path)

    def test_non_object_report_is_rejected(self):
        self.wrapper["report"] = []
        self.wrapper["sha256"] = digest([])
        self.save()
        with self.assertRaisesRegex(ValueError, "digest differs"):
            restored_boundary(self.path)
