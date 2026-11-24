import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from .contracts import Rejected
from .integrated_validate import validation_connection
from .validation_profile import ValidationProfile


class OperatorValidationTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.token = Path(self.directory.name) / "worker-token"
        self.token.write_text("selected-worker-token")
        self.profile = ValidationProfile(
            "sha256:" + "a" * 64, "sha256:" + "b" * 64, "c" * 64
        )
        self.observed = {
            "runtime": "compact-worker-v1",
            "qualified": True,
            "validation_profile": self.profile.data(),
        }
        environment = {
            "VIBEARR_RUNTIME_PROTOCOL": "compact-worker-v1",
            "VIBEARR_WORKER_IMAGE_ID": self.profile.image,
            "VIBEARR_EXECUTOR_TOKEN_FILE": str(self.token),
        }
        self.environment = patch.dict(os.environ, environment, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def connect(self):
        client = SimpleNamespace(capabilities=lambda: self.observed)
        with patch(
            "vibearr_executor.integrated_validate.Client", return_value=client
        ) as factory:
            result = validation_connection()
        factory.assert_called_once_with(
            "http://127.0.0.1:8791", "selected-worker-token", timeout=180
        )
        return result

    def test_compact_validation_needs_no_runner_secret_mount(self):
        _, profile = self.connect()
        self.assertEqual(profile, self.profile)

    def test_old_integrated_runtime_cannot_qualify_compact_validation(self):
        self.observed["runtime"] = "integrated-runner-v1"
        with self.assertRaises(Rejected):
            self.connect()

    def test_unqualified_current_boot_cannot_validate(self):
        self.observed["qualified"] = False
        with self.assertRaises(Rejected):
            self.connect()

    def test_other_worker_image_cannot_supply_validation_tools(self):
        os.environ["VIBEARR_WORKER_IMAGE_ID"] = "sha256:" + "d" * 64
        with self.assertRaises(Rejected):
            self.connect()
