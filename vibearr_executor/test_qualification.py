import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from .contracts import canonical
from .qualification import REQUIRED_CHECKS, capabilities
from .qualify import smoke_profiles
from .runtime import Runtime, durable


class QualificationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        self.image = "sha256:" + "a" * 64
        self.runtime = Runtime(self.directory, smoke_profiles(self.image))
        self.binding = {
            "executor_source_digest": "b" * 64,
            "kernel": "kernel-1",
            "engine_id": "engine-1",
            "images": {self.image: {"installed_contract": True}},
            "profiles": {"smoke-agent-v1": "c" * 64},
            "denied_ips": ["127.0.0.1"],
        }
        self.report = {
            "binding": self.binding,
            "checks": sorted(REQUIRED_CHECKS),
            "image_checks": {self.image: sorted(REQUIRED_CHECKS)},
        }
        durable(self.directory / "qualification.json", canonical(self.report))

    def tearDown(self):
        self.runtime.close()
        self.temporary.cleanup()

    def test_matching_actual_boundary_report_qualifies_only_runtime(self):
        with patch("vibearr_executor.qualification.binding", return_value=self.binding):
            result = capabilities(self.runtime)
        self.assertTrue(result["qualified"])
        self.assertFalse(result["native_agent_ready"])

    def test_material_source_engine_kernel_profile_or_egress_change_revokes(self):
        for key in (
            "executor_source_digest",
            "kernel",
            "engine_id",
            "profiles",
            "denied_ips",
        ):
            changed = copy.deepcopy(self.binding)
            changed[key] = "changed"
            with (
                self.subTest(key=key),
                patch("vibearr_executor.qualification.binding", return_value=changed),
            ):
                self.assertFalse(capabilities(self.runtime)["qualified"])

    def test_missing_installed_image_revokes(self):
        changed = copy.deepcopy(self.binding)
        changed["images"][self.image]["installed_contract"] = False
        with patch("vibearr_executor.qualification.binding", return_value=changed):
            self.assertFalse(capabilities(self.runtime)["qualified"])

    def test_unexercised_image_does_not_qualify(self):
        self.report["image_checks"] = {}
        durable(self.directory / "qualification.json", canonical(self.report))
        with patch("vibearr_executor.qualification.binding", return_value=self.binding):
            self.assertFalse(capabilities(self.runtime)["qualified"])

    def test_missing_required_boundary_check_does_not_qualify(self):
        self.report["checks"].pop()
        durable(self.directory / "qualification.json", canonical(self.report))
        with patch("vibearr_executor.qualification.binding", return_value=self.binding):
            self.assertFalse(capabilities(self.runtime)["qualified"])
