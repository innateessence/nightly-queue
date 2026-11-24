from copy import deepcopy
import unittest

from .audit import audit, inspect_containers, inspect_stack


class RuntimeInspectionTests(unittest.TestCase):
    def setUp(self):
        self.container = {
            "Name": "/checks-worker-1",
            "Id": "a" * 64,
            "Image": "sha256:" + "b" * 64,
            "Config": {
                "Labels": {
                    "com.docker.compose.service": "worker",
                    "com.docker.compose.project": "checks",
                },
                "Env": [
                    "UNRELATED_SECRET=must-not-appear-in-report",
                    "VIBEARR_WORKER_IMAGE_ID=sha256:" + "b" * 64,
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
            "Mounts": [
                {
                    "Type": "volume",
                    "Name": "checks_worker-control",
                    "Destination": "/control",
                    "RW": True,
                },
                {
                    "Type": "volume",
                    "Name": "checks_worker-secrets",
                    "Destination": "/run/vibearr/worker",
                    "RW": False,
                },
            ],
            "NetworkSettings": {"Networks": {"checks_worker-control": {}}},
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

    def test_worker_cannot_mount_the_runner_login_store(self):
        self.container["Mounts"].append(
            {
                "Type": "volume",
                "Name": "checks_runner-logins",
                "Destination": "/other",
                "RW": False,
            }
        )
        self.assertIn(
            "only worker control", str(inspect_containers([self.container])["errors"])
        )

    def test_worker_cannot_join_the_production_database_network(self):
        self.container["NetworkSettings"]["Networks"]["checks_database"] = {}
        self.assertIn(
            "networks differ", str(inspect_containers([self.container])["errors"])
        )

    def test_worker_channel_secret_mount_cannot_be_writable(self):
        self.container["Mounts"][1]["RW"] = True
        self.assertIn(
            "channel secrets are writable",
            str(inspect_containers([self.container])["errors"]),
        )

    def test_app_may_initialize_worker_channel_secrets(self):
        self.container["Config"]["Labels"]["com.docker.compose.service"] = "app"
        self.container["Mounts"] = [self.container["Mounts"][1]]
        self.container["Mounts"][0]["RW"] = True
        self.container["NetworkSettings"]["Networks"] = {
            "checks_database": {},
            "checks_runner-api": {},
            "checks_app-egress": {},
        }
        self.assertEqual(inspect_containers([self.container])["errors"], [])

    def test_runner_worker_channel_secret_mount_cannot_be_writable(self):
        self.container["Config"]["Labels"]["com.docker.compose.service"] = "runner"
        self.container["Mounts"][1]["RW"] = True
        errors = inspect_containers([self.container])["errors"]
        self.assertIn("channel secrets are writable", str(errors))

    def test_partial_stack_cannot_pass_actual_container_audit(self):
        self.assertIn("exactly one app", str(inspect_stack([self.container])["errors"]))

    def test_foreign_secret_volume_cannot_be_hidden_by_its_logical_suffix(self):
        self.container["Mounts"][1]["Name"] = "production_worker-secrets"
        errors = str(inspect_containers([self.container])["errors"])
        self.assertIn("private volume belongs to a different project", errors)

    def test_duplicate_worker_cannot_replace_a_missing_service(self):
        containers = [deepcopy(self.container) for _ in range(4)]
        self.assertIn("exactly one app", str(inspect_stack(containers)["errors"]))

    def test_containers_from_different_projects_cannot_form_a_stack(self):
        other = deepcopy(self.container)
        other["Config"]["Labels"]["com.docker.compose.project"] = "other"
        self.assertIn(
            "one Compose project", str(inspect_stack([self.container, other])["errors"])
        )


class ComposeAuditTests(unittest.TestCase):
    def setUp(self):
        from .audit import NETWORKS

        self.config = {
            "services": {},
            "networks": {
                name: {"internal": True}
                for name in ("database", "runner-api", "worker-control")
            },
        }
        for name, networks in NETWORKS.items():
            service = {
                "read_only": True,
                "networks": dict.fromkeys(networks),
                "environment": {},
                "volumes": [],
            }
            self.config["services"][name] = service
        for name, mounts in {
            "runner": (
                "runner-data",
                "runner-secrets",
                "runner-logins",
                "worker-secrets",
            ),
            "worker": ("worker-control", "worker-secrets"),
        }.items():
            service = self.config["services"][name]
            service["volumes"] = [
                {
                    "type": "volume",
                    "source": source,
                    "target": "/" + source,
                    "read_only": source == "worker-secrets",
                }
                for source in mounts
            ]
            service["environment"] = {
                "VIBEARR_RUNTIME_PROTOCOL": "compact-worker-v1",
                "VIBEARR_" + name.upper() + "_IMAGE_ID": "sha256:" + "a" * 64,
            }

    def test_exact_compact_stack_passes(self):
        self.assertEqual(audit(self.config), [])

    def test_hidden_fifth_service_is_rejected(self):
        self.config["services"]["proxy"] = {"read_only": True}
        self.assertIn("exactly app", str(audit(self.config)))

    def test_runner_execution_capabilities_are_rejected(self):
        self.config["services"]["runner"]["cap_add"] = ["CAP_NET_ADMIN"]
        self.assertIn("task execution capabilities", str(audit(self.config)))

    def test_worker_cannot_mount_production_credentials(self):
        self.config["services"]["worker"]["volumes"].append(
            {"type": "volume", "source": "database-secrets", "target": "/db"}
        )
        self.assertIn("only worker control", str(audit(self.config)))

    def test_worker_private_network_cannot_have_external_egress(self):
        self.config["networks"]["worker-control"]["internal"] = False
        self.assertIn(
            "trusted service network must be internal", str(audit(self.config))
        )

    def test_runner_cannot_receive_production_encryption_key(self):
        self.config["services"]["runner"]["environment"][
            "VIBEARR_CREDENTIAL_KEY"
        ] = "secret"
        self.assertIn("application credentials are forbidden", str(audit(self.config)))

    def test_worker_cannot_receive_a_publication_token(self):
        self.config["services"]["worker"]["environment"]["GH_TOKEN"] = "secret"
        self.assertIn(
            "publication and controller credentials are forbidden",
            str(audit(self.config)),
        )

    def test_integrated_runtime_cannot_be_relabeled_as_a_worker(self):
        self.config["services"]["worker"]["environment"][
            "VIBEARR_RUNTIME_PROTOCOL"
        ] = "integrated-runner-v1"
        self.assertIn("select the compact worker boundary", str(audit(self.config)))
