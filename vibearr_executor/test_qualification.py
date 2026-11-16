import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from .contracts import canonical
from .qualification import REQUIRED_CHECKS, binding, capabilities
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


class HostTopologyQualificationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.image = "sha256:" + "a" * 64
        self.runtime = Runtime(
            Path(temporary.name), smoke_profiles(self.image), denied_ips=("10.0.0.99",)
        )
        self.addCleanup(self.runtime.close)
        self.hosts = {"127.0.0.1", "10.0.0.2", "8.8.8.8"}
        addresses = patch.object(
            Runtime, "host_addresses", side_effect=lambda: self.hosts
        )
        addresses.start()
        self.addCleanup(addresses.stop)
        engine = patch.object(self.runtime.docker, "run", side_effect=self.engine_reply)
        engine.start()
        self.addCleanup(engine.stop)
        self.initial_binding = binding(self.runtime)
        report = {
            "binding": self.initial_binding,
            "checks": sorted(REQUIRED_CHECKS),
            "image_checks": {self.image: sorted(REQUIRED_CHECKS)},
        }
        durable(self.runtime.directory / "qualification.json", canonical(report))

    def engine_reply(self, *args):
        if args[0] == "version":
            return canonical({"Version": "test", "ApiVersion": "test"})
        if args[0] == "info":
            return canonical(
                {
                    "ID": "test",
                    "Driver": "overlay2",
                    "CgroupVersion": "2",
                    "SecurityOptions": [],
                }
            )
        self.assertEqual(args[:2], ("image", "inspect"))
        return canonical(
            [
                {
                    "Id": self.image,
                    "Config": {"Labels": {"io.vibearr.executor.protocol": "1"}},
                }
            ]
        )

    def test_private_bridge_changes_keep_qualification_and_full_enforcement_denies(
        self,
    ):
        self.assertTrue(capabilities(self.runtime)["qualified"])
        self.hosts.add("172.18.0.1")
        self.assertTrue(capabilities(self.runtime)["qualified"])
        self.assertEqual(binding(self.runtime), self.initial_binding)
        self.assertIn("172.18.0.1", self.runtime.current_denied_ips())
        self.hosts.remove("172.18.0.1")
        self.assertTrue(capabilities(self.runtime)["qualified"])

    def test_configured_private_deny_change_revokes_qualification(self):
        self.assertIn("10.0.0.99", self.initial_binding["denied_ips"])
        self.runtime.configured_ips = ("10.0.0.100",)
        self.assertFalse(capabilities(self.runtime)["qualified"])

    def test_public_host_address_addition_or_removal_revokes_qualification(self):
        for hosts in (self.hosts | {"1.1.1.1"}, self.hosts - {"8.8.8.8"}):
            with self.subTest(hosts=hosts):
                self.hosts = hosts
                self.assertFalse(capabilities(self.runtime)["qualified"])
