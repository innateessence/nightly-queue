import copy
from datetime import datetime, timezone
import json
from pathlib import Path
import random
import subprocess
import sys
import tempfile
import unittest
import struct
import zlib
from unittest.mock import patch
from uuid import uuid4
from types import SimpleNamespace

from vibearr_executor.artifacts import pack
from vibearr_executor.contracts import Limits, Rejected
from vibearr_executor.local_services import (
    LocalServices,
    BrowserEvidenceError,
    browser_profile,
    read_report,
    stop_services,
    source_tree,
)
from vibearr_runner.browser_receipts import (
    VIEWPORTS,
    capture_worker_receipt,
    import_worker_capture,
    validate_worker_report,
    validate_page_path,
)


def fixture_png(*, reverse=False, width=1440, height=1000):
    def chunk(kind, payload):
        return (
            struct.pack(">I", len(payload))
            + kind
            + payload
            + struct.pack(">I", zlib.crc32(kind + payload))
        )

    pixels = random.Random(3 if reverse else 2).randbytes(width * 3 * 16)
    rows = [
        b"\x00" + pixels[index * width * 3 : (index + 1) * width * 3]
        for index in range(16)
    ]
    raw = b"".join(rows[index % 16] for index in range(height))
    header = chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
    return (
        b"\x89PNG\r\n\x1a\n"
        + header
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


class WorkerBrowserReceiptTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.capture = self.root / ("capture-" + uuid4().hex)
        self.capture.mkdir()
        self.binding = {
            "schema_version": 1,
            "revision": "a" * 40,
            "tree_sha": "b" * 40,
            "source_digest": "c" * 64,
            "application_image": "sha256:" + "d" * 64,
            "browser_image": "sha256:" + "d" * 64,
        }
        self.worker = {
            "run_id": str(uuid4()),
            "operation_id": str(uuid4()),
            "profile_id": "browser-ui",
            "profile_digest": "e" * 64,
            "execution_identity": {
                "runtime": "compact-worker-v1",
                "protocol_version": 2,
                "instance_id": str(uuid4()),
                "worker_boot": str(uuid4()),
                "binding_digest": "f" * 64,
            },
        }
        frames = []
        for view, viewport in VIEWPORTS.items():
            for theme in ("light", "dark"):
                path = self.capture / f"queue-{view}-{theme}.png"
                path.write_bytes(fixture_png(**viewport))
                frames.append(
                    capture_worker_receipt(
                        self.binding,
                        self.worker,
                        path,
                        receipt_path=Path("/screenshots")
                        / self.capture.name
                        / path.name,
                        url="http://127.0.0.1:8765/?theme=" + theme,
                        viewport=viewport,
                        theme=theme,
                    )
                )
        self.report = {
            "schema_version": 3,
            "kind": "compact-worker-browser-acceptance",
            "browser": "chromium",
            "source_binding": self.binding,
            "worker_binding": self.worker,
            "screenshots": frames,
            "page_errors": [],
            "failed_requests": [],
            "boundary_checks": {
                "control_paths_denied": True,
                "credentials_scoped": True,
                "source_write_denied": True,
            },
        }
        (self.capture / "browser.json").write_text(json.dumps(self.report))

    def archive(self):
        return pack(
            {
                str(path.relative_to(self.root)): (0o644, path.read_bytes())
                for path in self.capture.iterdir()
            }
        )

    def validate(self, report=None):
        return validate_worker_report(
            report or self.report,
            binding=self.binding,
            worker_binding=self.worker,
            storage_root=self.root,
        )

    def test_complete_bound_worker_capture_is_accepted(self):
        self.assertEqual(len(self.validate()), 4)

    def test_previous_worker_boot_is_rejected(self):
        report = copy.deepcopy(self.report)
        report["worker_binding"]["execution_identity"]["worker_boot"] = str(uuid4())
        with self.assertRaisesRegex(ValueError, "expected source or worker"):
            self.validate(report)

    def test_frame_for_another_source_is_rejected(self):
        report = copy.deepcopy(self.report)
        report["screenshots"][0]["source_digest"] = "9" * 64
        with self.assertRaisesRegex(ValueError, "one source/image binding"):
            self.validate(report)

    def test_modified_capture_bytes_are_rejected(self):
        path = self.capture / "queue-mobile-dark.png"
        path.write_bytes(path.read_bytes() + b"changed")
        with self.assertRaisesRegex(ValueError, "PNG"):
            self.validate()

    def test_browser_errors_prevent_acceptance(self):
        report = copy.deepcopy(self.report)
        report["failed_requests"] = ["http://unapproved.invalid/"]
        with self.assertRaisesRegex(ValueError, "Browser errors"):
            self.validate(report)

    def test_header_only_junk_cannot_claim_to_be_a_png(self):
        path = self.capture / "queue-mobile-dark.png"
        path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"not pixels" * 2000)
        with self.assertRaisesRegex(ValueError, "PNG"):
            self.validate()

    def test_missing_viewport_prevents_acceptance(self):
        report = copy.deepcopy(self.report)
        report["screenshots"].pop()
        with self.assertRaisesRegex(ValueError, "all desktop/mobile"):
            self.validate(report)

    def test_unmeasured_control_boundary_prevents_acceptance(self):
        report = copy.deepcopy(self.report)
        report["boundary_checks"]["control_paths_denied"] = False
        with self.assertRaisesRegex(ValueError, "boundaries were not measured"):
            self.validate(report)

    def test_import_materializes_all_images_and_reuses_identical_capture(self):
        target = self.root / "imported"
        for _ in range(2):
            frames = import_worker_capture(
                self.report,
                self.archive(),
                binding=self.binding,
                worker_binding=self.worker,
                artifact_root=target,
            )
            self.assertEqual(len(frames), 4)
        self.assertEqual(len(list((target / self.capture.name).iterdir())), 5)

    def test_archive_cannot_smuggle_extra_files(self):
        (self.capture / "private.txt").write_text("unexpected")
        with self.assertRaisesRegex(ValueError, "unexpected files"):
            import_worker_capture(
                self.report,
                self.archive(),
                binding=self.binding,
                worker_binding=self.worker,
                artifact_root=self.root / "imported",
            )

    def test_import_rejects_existing_capture_tampering(self):
        target = self.root / "imported"
        arguments = {
            "binding": self.binding,
            "worker_binding": self.worker,
            "artifact_root": target,
        }
        archive = self.archive()
        import_worker_capture(self.report, archive, **arguments)
        (target / self.capture.name / "queue-desktop-light.png").write_bytes(
            b"tampered"
        )
        with self.assertRaisesRegex(ValueError, "immutable"):
            import_worker_capture(self.report, archive, **arguments)

    def test_report_cannot_claim_an_external_page(self):
        report = copy.deepcopy(self.report)
        report["screenshots"][0]["url"] = "http://example.com:8765/"
        with self.assertRaisesRegex(ValueError, "selected Docker application"):
            self.validate(report)

    def test_report_cannot_mix_pages(self):
        report = copy.deepcopy(self.report)
        report["screenshots"][0]["url"] = "http://127.0.0.1:8765/activity/?theme=light"
        with self.assertRaisesRegex(ValueError, "same selected page"):
            self.validate(report)

    def test_capture_must_match_the_declared_theme(self):
        report = copy.deepcopy(self.report)
        report["screenshots"][0]["url"] = "http://127.0.0.1:8765/?theme=dark"
        with self.assertRaisesRegex(ValueError, "theme differs"):
            self.validate(report)

    def test_old_capture_time_is_rejected_for_the_same_operation(self):
        operation = self.worker["operation_id"]
        directory = self.root / operation
        directory.mkdir()
        now = datetime.now(timezone.utc).timestamp()
        (directory / "browser-window.json").write_text(
            json.dumps({"not_before": now + 1, "deadline": now + 30, "page_path": "/"})
        )
        services = LocalServices()
        services.runtime = SimpleNamespace(path=lambda value: self.root / value)
        with self.assertRaisesRegex(Rejected, "outside this operation"):
            services._validate_window(operation, self.report)


class LocalServiceTests(unittest.TestCase):
    def test_source_tree_matches_the_git_empty_tree(self):
        self.assertEqual(
            source_tree(pack({}), 40), "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
        )

    def test_source_tree_binds_file_contents_and_executable_mode(self):
        content = source_tree(pack({"app/main.py": (0o644, b"input")}), 40)
        changed = source_tree(pack({"app/main.py": (0o644, b"changed")}), 40)
        executable = source_tree(pack({"app/main.py": (0o755, b"input")}), 40)
        self.assertNotEqual(content, changed)
        self.assertNotEqual(content, executable)

    def test_a_canceled_capture_cannot_be_supplied_to_a_visual_reviewer(self):
        services = LocalServices()
        services.runtime = SimpleNamespace(
            record=lambda operation: {
                "state": "exited",
                "descendants_terminated": True,
                "result": {
                    "terminated": True,
                    "exit_code": 0,
                    "timed_out": False,
                    "canceled": True,
                },
            }
        )
        with self.assertRaisesRegex(Rejected, "completed, terminated"):
            services._review_inputs(
                {}, {"operation_id": str(uuid4()), "archive_digest": "a" * 64}
            )

    def test_only_registered_local_page_paths_can_be_selected(self):
        for path in (
            "https://example.com/",
            "//example.com/",
            "/credentials/",
            "/activity/?next=http://example.com/",
        ):
            with self.subTest(path=path):
                with self.assertRaises(ValueError):
                    validate_page_path(path)

    def test_missing_capture_is_rejected_after_confirmed_service_cleanup(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            operation = str(uuid4())
            (root / operation).mkdir()
            services = LocalServices(root=root)
            services.runtime = SimpleNamespace(
                home=root / "home", cancel=SimpleNamespace(is_set=lambda: False)
            )
            services.active = operation
            with self.assertRaises(BrowserEvidenceError):
                services.after_phase(operation)
            self.assertEqual(services.active, "")
            self.assertFalse((root / operation).exists())

    def test_linked_browser_report_is_not_read(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "browser.json"
            path.symlink_to(Path(directory) / "private-control")
            with self.assertRaises(OSError):
                read_report(path)

    def test_oversized_browser_report_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "browser.json"
            path.write_bytes(b"x" * (1024**2 + 1))
            with self.assertRaisesRegex(Rejected, "bounded regular"):
                read_report(path)

    def test_browser_profile_has_no_public_egress_or_backend_credential(self):
        profile = browser_profile("sha256:" + "a" * 64, Limits(2, 4096, 128, 2048))
        self.assertEqual(profile.phase, "validation")
        self.assertEqual(profile.network_hosts, ())
        self.assertEqual(profile.backend_secret_env, "")

    def test_phase_cleanup_rejects_another_operation(self):
        services = LocalServices()
        services.active = str(uuid4())
        with self.assertRaisesRegex(Rejected, "different operation"):
            services.after_phase(str(uuid4()))

    def test_process_group_is_stopped_and_reaped(self):
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            start_new_session=True,
        )
        self.addCleanup(lambda: process.kill() if process.poll() is None else None)
        stop_services([process])
        self.assertIsNotNone(process.poll())

    def test_uncertain_service_cleanup_keeps_operation_owned(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        services = LocalServices(root=Path(temporary.name))
        services.runtime = object()
        operation = str(uuid4())
        services.active = operation
        with patch(
            "vibearr_executor.local_services.stop_services",
            side_effect=RuntimeError("uncertain"),
        ):
            with self.assertRaisesRegex(RuntimeError, "uncertain"):
                services.after_phase(operation, capture=False)
        self.assertEqual(services.active, operation)
