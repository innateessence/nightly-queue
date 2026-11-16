import importlib
import os
from unittest.mock import patch

from django.test import TestCase


class RenameCompatibilityTests(TestCase):
    def test_legacy_django_settings_import_is_same_module(self):
        self.assertIs(
            importlib.import_module("nightly_queue.settings"),
            importlib.import_module("vibearr_app.settings"),
        )

    def test_runtime_alias_keeps_class_and_patch_identity(self):
        legacy = importlib.import_module("coding_runner.client")
        canonical = importlib.import_module("vibearr_runner.client")
        self.assertIs(legacy, canonical)
        self.assertIs(legacy.Client, canonical.Client)
        with patch("coding_runner.client.time.monotonic", return_value=12):
            self.assertEqual(canonical.time.monotonic(), 12)

    def test_existing_settings_name_still_loads_canonical_url_configuration(self):
        module = importlib.import_module("nightly_queue.settings")
        self.assertEqual(module.ROOT_URLCONF, "vibearr_app.urls")

    def test_independent_runtime_supports_canonical_key_precedence(self):
        from vibearr_runner.environment import getenv

        values = {
            "VIBEARR_RUNNER_API_TOKEN": "new-secret",
            "NQ_RUNNER_API_TOKEN": "old-secret",
        }
        with patch.dict(os.environ, values, clear=True):
            with self.assertWarns(RuntimeWarning) as context:
                self.assertEqual(getenv("NQ_RUNNER_API_TOKEN"), "new-secret")
        self.assertNotIn("new-secret", str(context.warning))
        self.assertNotIn("old-secret", str(context.warning))

    def test_queue_uses_vibearr_brand(self):
        response = self.client.get("/projects/")
        self.assertContains(response, "vibearr home")
        self.assertNotContains(response, "Nightly home")
