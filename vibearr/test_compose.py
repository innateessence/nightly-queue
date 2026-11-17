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

    def test_default_stack_has_exactly_three_services(self):
        self.assertEqual(
            set(self.configuration()["services"]), {"app", "postgres", "runner"}
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

    def test_journal_volume_keeps_legacy_absolute_paths_accessible(self):
        mounts = self.configuration()["services"]["runner"]["volumes"]
        targets = {entry["target"]: entry["source"] for entry in mounts}
        self.assertEqual(targets["/var/lib/vibearr"], targets["/var/lib/coding-runner"])

    def test_runner_and_database_use_declared_named_volumes(self):
        for name, service in self.configuration()["services"].items():
            for mount in service.get("volumes", []):
                with self.subTest(service=name, mount=mount["target"]):
                    self.assertEqual(mount["type"], "volume")

    def test_ollama_is_not_a_managed_service_or_volume(self):
        config = self.configuration()
        self.assertFalse(any("ollama" in name for name in config["services"]))
        self.assertFalse(any("ollama" in name for name in config["volumes"]))
