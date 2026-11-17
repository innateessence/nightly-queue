from copy import deepcopy
import unittest

from .audit import inspect_containers


class RuntimeInspectionTests(unittest.TestCase):
    def setUp(self):
        self.container = {
            "Name": "/checks-runner-1",
            "Id": "a" * 64,
            "Image": "sha256:" + "b" * 64,
            "Config": {
                "Labels": {"com.docker.compose.service": "runner"},
                "Env": [
                    "UNRELATED_SECRET=must-not-appear-in-report",
                    "VIBEARR_RUNNER_IMAGE_ID=sha256:" + "b" * 64,
                ],
            },
            "HostConfig": {
                "ReadonlyRootfs": True,
                "CapDrop": ["ALL"],
                "SecurityOpt": ["no-new-privileges:true"],
                "Memory": 4096 * 1024**2,
                "MemorySwap": 4096 * 1024**2,
                "NanoCpus": 2_000_000_000,
                "PidsLimit": 128,
            },
            "Mounts": [],
        }

    def test_runtime_inspection_does_not_export_environment_values(self):
        report = inspect_containers([self.container])
        self.assertEqual(report["errors"], [])
        self.assertNotIn("must-not-appear-in-report", str(report))

    def test_runtime_host_bind_is_rejected(self):
        self.container["Mounts"] = [
            {"Type": "bind", "Destination": "/app", "RW": False}
        ]
        self.assertIn(
            "runtime host bind", inspect_containers([self.container])["errors"][0]
        )

    def test_actual_memory_must_match_the_registered_worker(self):
        self.container["HostConfig"]["Memory"] = 0
        report = inspect_containers([self.container])
        self.assertIn("actual worker ceilings differ", report["errors"][0])

    def test_declared_image_must_match_the_running_worker(self):
        self.container["Image"] = "sha256:" + "c" * 64
        report = inspect_containers([self.container])
        self.assertIn("declared worker image differs", report["errors"][0])

    def test_capability_prefix_cannot_bypass_the_sys_admin_rejection(self):
        self.container["HostConfig"]["CapAdd"] = ["CAP_SYS_ADMIN"]
        report = inspect_containers([self.container])
        self.assertIn("forbidden runtime capabilities", report["errors"][0])

    def test_a_sibling_container_network_namespace_is_rejected(self):
        self.container["HostConfig"]["NetworkMode"] = "container:sibling"
        report = inspect_containers([self.container])
        self.assertIn("shared network namespace", report["errors"][0])

    def test_inspection_leaves_the_operator_supplied_facts_unchanged(self):
        original = deepcopy(self.container)
        inspect_containers([self.container])
        self.assertEqual(self.container, original)
