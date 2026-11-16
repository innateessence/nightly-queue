import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


@unittest.skipUnless(shutil.which("docker"), "Compose checks run on the Docker host")
class ComposeNamingTests(unittest.TestCase):
    def configuration(self, values):
        root = Path(__file__).resolve().parent.parent
        if not (root / "docker-compose.yml").exists():
            self.skipTest("Source deployment is not in the installed CLI package")
        with tempfile.NamedTemporaryFile(mode="w") as environment:
            environment.write(values)
            environment.flush()
            result = subprocess.run(
                [
                    "docker",
                    "compose",
                    "--env-file",
                    environment.name,
                    "--profile",
                    "runner",
                    "config",
                    "--format",
                    "json",
                ],
                cwd=root,
                env={"PATH": os.environ["PATH"]},
                capture_output=True,
                text=True,
                check=True,
            )
        return json.loads(result.stdout)

    def test_minimal_legacy_environment_keeps_existing_storage_identity(self):
        values = "POSTGRES_PASSWORD=old-db\nNQ_API_TOKEN=old-api\nNQ_CREDENTIAL_KEY=old-key\nNQ_RUNNER_API_TOKEN=old-runner\n"
        config = self.configuration(values)
        app = config["services"]["vibearr"]["environment"]
        self.assertEqual(app["POSTGRES_DB"], "nightly_queue")
        self.assertEqual(app["POSTGRES_USER"], "nightly_queue")
        self.assertEqual(app["VIBEARR_API_TOKEN"], "old-api")
        self.assertEqual(app["VIBEARR_CREDENTIAL_KEY"], "old-key")
        self.assertEqual(
            config["volumes"]["postgres-data"]["name"], "nightly-queue_postgres-data"
        )

    def test_fresh_example_selects_canonical_storage_identity(self):
        example = Path(__file__).resolve().parent.parent / ".env.example"
        if not example.exists():
            self.skipTest(
                "Source environment example is not in the installed CLI package"
            )
        config = self.configuration(example.read_text())
        app = config["services"]["vibearr"]["environment"]
        self.assertEqual(config["name"], "vibearr")
        self.assertEqual(app["VIBEARR_POSTGRES_DB"], "vibearr")
        self.assertEqual(app["VIBEARR_POSTGRES_USER"], "vibearr")
        self.assertEqual(
            config["volumes"]["postgres-data"]["name"], "vibearr_postgres-data"
        )

    def test_explicit_legacy_project_name_keeps_custom_volume_prefix(self):
        config = self.configuration(
            "COMPOSE_PROJECT_NAME=private-queue\nPOSTGRES_PASSWORD=old-db\n"
        )
        self.assertEqual(
            config["volumes"]["postgres-data"]["name"], "private-queue_postgres-data"
        )

    def test_explicit_volume_project_is_independent_of_service_project(self):
        config = self.configuration(
            "COMPOSE_PROJECT_NAME=vibearr\nVIBEARR_DATA_PROJECT=old-deploy\n"
        )
        self.assertEqual(
            config["volumes"]["postgres-data"]["name"], "old-deploy_postgres-data"
        )

    def test_legacy_runner_mount_preserves_saved_absolute_paths(self):
        config = self.configuration("NQ_RUNNER_API_TOKEN=existing\n")
        runner = config["services"]["vibearr-runner"]
        self.assertEqual(
            runner["environment"]["VIBEARR_RUNNER_STATE_DIR"], "/var/lib/coding-runner"
        )
        self.assertEqual(runner["volumes"][0]["target"], "/var/lib/coding-runner")
        self.assertEqual(
            config["volumes"]["runner-data"]["name"], "nightly-queue_runner-data"
        )

    def test_fresh_runner_uses_canonical_path_in_existing_volume_mapping(self):
        values = "COMPOSE_PROJECT_NAME=vibearr\nVIBEARR_DATA_PROJECT=vibearr\nVIBEARR_RUNNER_STATE_DIR=/var/lib/vibearr\n"
        config = self.configuration(values)
        runner = config["services"]["vibearr-runner"]
        self.assertEqual(
            runner["environment"]["VIBEARR_RUNNER_STATE_DIR"], "/var/lib/vibearr"
        )
        self.assertEqual(runner["volumes"][0]["target"], "/var/lib/vibearr")
