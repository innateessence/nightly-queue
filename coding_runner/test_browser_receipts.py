import json
import os
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

from vibearr_runner.browser_receipts import (
    VIEWPORTS,
    adopt_capture,
    capture_receipt,
    collect_report,
    validate_binding,
    validate_receipt,
)
from vibearr_runner.merge_acceptance import capture_page


class DockerBrowserReceiptTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.root = self.directory / "screenshots"
        self.capture = self.root / ("capture-" + uuid4().hex)
        self.capture.mkdir(parents=True)
        self.binding = {
            "schema_version": 1,
            "revision": "a" * 40,
            "tree_sha": "b" * 40,
            "source_digest": "c" * 64,
            "application_image": "sha256:" + "d" * 64,
            "browser_image": "sha256:" + "e" * 64,
        }

    def frame(self, name="desktop", theme="light"):
        path = self.capture / f"queue-{name}-{theme}.png"
        path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"fixture image bytes" * 1000)
        return capture_receipt(
            self.binding,
            path,
            receipt_path=path,
            url="http://vibearr:8000/?theme=" + theme,
            viewport=VIEWPORTS[name],
            theme=theme,
        )

    def validate(self, frame):
        return validate_receipt(
            frame,
            self.binding["revision"],
            expected_tree=self.binding["tree_sha"],
            artifact_root=self.root,
        )

    def report(self):
        frames = [
            self.frame(name, theme) for name in VIEWPORTS for theme in ("light", "dark")
        ]
        path = self.capture / "browser.json"
        path.write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "kind": "docker-browser-acceptance",
                    "source_binding": self.binding,
                    "screenshots": frames,
                    "page_errors": [],
                    "failed_requests": [],
                }
            )
        )
        path.chmod(0o600)
        return path

    def test_bound_png_from_the_validated_tree_is_accepted(self):
        self.assertEqual(
            self.validate(self.frame())["tree_sha"], self.binding["tree_sha"]
        )

    def test_other_tree_cannot_substitute_for_the_validated_ui(self):
        frame = self.frame()
        frame["tree_sha"] = "f" * 40
        with self.assertRaisesRegex(ValueError, "validated UI tree"):
            self.validate(frame)

    def test_changed_png_cannot_reuse_its_capture_receipt(self):
        frame = self.frame()
        Path(frame["path"]).write_bytes(
            b"\x89PNG\r\n\x1a\n" + b"different image" * 1000
        )
        with self.assertRaisesRegex(ValueError, "differs"):
            self.validate(frame)

    def test_image_tags_cannot_substitute_for_inspected_image_ids(self):
        with self.assertRaisesRegex(ValueError, "immutable image IDs"):
            validate_binding(
                {**self.binding, "application_image": "vibearr-app:latest"}
            )

    def test_host_browser_receipts_cannot_qualify_docker_acceptance(self):
        with self.assertRaisesRegex(ValueError, "Docker browser"):
            self.validate({"schema_version": 1, "browser": "/opt/google/chrome/chrome"})

    def test_host_capture_helper_cannot_launch_a_browser(self):
        with self.assertRaisesRegex(RuntimeError, "retired"):
            capture_page(
                source_checkout="/unused",
                revision="a" * 40,
                url="http://127.0.0.1:8000",
                output="/unused.png",
                browser_profile="/unused",
            )

    def test_receipt_cannot_read_an_arbitrary_file_outside_browser_artifacts(self):
        frame = self.frame()
        frame["path"] = str(self.directory / "outside.png")
        with self.assertRaisesRegex(ValueError, "artifact volume"):
            self.validate(frame)

    def test_linked_png_is_rejected_without_reading_its_target(self):
        frame = self.frame()
        path = Path(frame["path"])
        path.unlink()
        path.symlink_to(self.directory / "private-sentinel")
        with self.assertRaises(OSError):
            self.validate(frame)

    def test_complete_report_is_collected_into_private_run_state(self):
        run = str(uuid4())
        state = self.directory / "runner-state"
        output = collect_report(self.report(), run, state, artifact_root=self.root)
        records = json.loads(Path(output["path"]).read_bytes())
        self.assertEqual(len(records[run]), 4)
        self.assertEqual(Path(output["path"]).stat().st_mode & 0o777, 0o600)

    def test_failed_browser_requests_prevent_report_collection(self):
        path = self.report()
        report = json.loads(path.read_bytes())
        report["failed_requests"] = ["http://vibearr:8000/missing"]
        path.write_text(json.dumps(report))
        with self.assertRaisesRegex(ValueError, "Browser errors"):
            collect_report(
                path, str(uuid4()), self.directory / "state", artifact_root=self.root
            )

    def test_duplicate_capture_modes_do_not_satisfy_complete_report_coverage(self):
        path = self.report()
        report = json.loads(path.read_bytes())
        report["screenshots"][3] = report["screenshots"][0]
        path.write_text(json.dumps(report))
        with self.assertRaisesRegex(ValueError, "coverage"):
            collect_report(
                path, str(uuid4()), self.directory / "state", artifact_root=self.root
            )

    def test_import_normalizes_only_the_selected_complete_capture(self):
        path = self.report()
        adopt_capture(self.capture.name, artifact_root=self.root, uid=os.getuid())
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.capture.stat().st_mode & 0o777, 0o700)

    def test_import_rejects_extra_files_without_changing_the_capture(self):
        self.report()
        unexpected = self.capture / "unexpected.txt"
        unexpected.write_text("untrusted extra file")
        unexpected.chmod(0o644)
        with self.assertRaisesRegex(ValueError, "exactly one complete"):
            adopt_capture(self.capture.name, artifact_root=self.root, uid=os.getuid())
        self.assertEqual(unexpected.stat().st_mode & 0o777, 0o644)
