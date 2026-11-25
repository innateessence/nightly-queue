import json
import os
from pathlib import Path
import unittest


@unittest.skipUnless(
    os.environ.get("VIBEARR_COMPOSE_CONFIG"),
    "Rendered Compose facts are supplied by the Docker check harness",
)
class ComposeTests(unittest.TestCase):
    def configuration(self):
        return json.loads(Path(os.environ["VIBEARR_COMPOSE_CONFIG"]).read_text())

    def test_default_stack_has_exactly_four_services(self):
        self.assertEqual(
            set(self.configuration()["services"]),
            {"app", "postgres", "runner", "worker"},
        )

    def test_runner_cannot_access_database_network_or_app_secrets(self):
        config = self.configuration()
        runner = config["services"]["runner"]
        self.assertNotIn("database", runner["networks"])
        sources = {entry["source"] for entry in runner["volumes"]}
        self.assertTrue(
            sources.isdisjoint(
                {"postgres-data", "application-secrets", "database-secrets"}
            )
        )

    def test_only_app_publishes_a_host_port(self):
        for name, service in self.configuration()["services"].items():
            with self.subTest(service=name):
                self.assertEqual(bool(service.get("ports")), name == "app")

    def test_worker_has_only_execution_control_and_dedicated_channel_secrets(self):
        worker = self.configuration()["services"]["worker"]
        self.assertEqual(set(worker["networks"]), {"worker-control"})
        mounts = {mount["source"]: mount for mount in worker["volumes"]}
        self.assertEqual(set(mounts), {"worker-control", "worker-secrets"})
        self.assertTrue(mounts["worker-secrets"]["read_only"])

    def test_only_worker_receives_task_network_and_uid_capabilities(self):
        services = self.configuration()["services"]
        self.assertIn("NET_ADMIN", services["worker"]["cap_add"])
        self.assertIn("SETPCAP", services["worker"]["cap_add"])
        self.assertNotIn("NET_ADMIN", services["runner"]["cap_add"])
        self.assertNotIn("SETPCAP", services["runner"]["cap_add"])

    def test_runner_and_worker_use_distinct_actual_image_bindings(self):
        services = self.configuration()["services"]
        self.assertIn("VIBEARR_RUNNER_IMAGE_ID", services["runner"]["environment"])
        self.assertIn("VIBEARR_WORKER_IMAGE_ID", services["worker"]["environment"])
        self.assertEqual(
            services["runner"]["environment"]["VIBEARR_EXECUTOR_URL"],
            "http://worker:8791",
        )

    def test_mediator_passes_explicit_private_controller_identity(self):
        environment = self.configuration()["services"]["runner"]["environment"]
        self.assertEqual(environment["VIBEARR_RUNNER_STATE_DIR"], "/var/lib/vibearr")
        self.assertEqual(
            environment["VIBEARR_RUNNER_API_TOKEN_FILE"],
            "/run/vibearr/runner/api-token",
        )

    def test_startup_does_not_depend_on_qualified_worker_or_running_mediation(self):
        services = self.configuration()["services"]
        self.assertEqual(set(services["worker"]["depends_on"]), {"app"})
        self.assertEqual(set(services["runner"]["depends_on"]), {"app", "worker"})
        self.assertEqual(services["worker"]["healthcheck"]["test"][-1], "health")

    def test_runner_journal_has_one_canonical_mount(self):
        mounts = self.configuration()["services"]["runner"]["volumes"]
        targets = {entry["target"]: entry["source"] for entry in mounts}
        self.assertEqual(targets["/var/lib/vibearr"], "runner-data")
        self.assertEqual(list(targets.values()).count("runner-data"), 1)

    def test_runner_and_database_use_declared_named_volumes(self):
        for name, service in self.configuration()["services"].items():
            for mount in service.get("volumes", []):
                with self.subTest(service=name, mount=mount["target"]):
                    self.assertEqual(mount["type"], "volume")

    def test_ollama_is_not_a_managed_service_or_volume(self):
        config = self.configuration()
        self.assertFalse(any("ollama" in name for name in config["services"]))
        self.assertFalse(any("ollama" in name for name in config["volumes"]))
