import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from .config import Config, repository_name


class ConfigTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "projects.json"
        self.path.write_text(
            json.dumps({"example/repo": {"validation": [["python", "-m", "unittest"]]}})
        )
        env = {
            "NQ_API_TOKEN": "queue-secret",
            "GH_TOKEN": "github-secret",
            "OPENAI_API_KEY": "openai-secret",
            "NQ_RUNNER_PROJECTS": str(self.path),
        }
        replacement = patch.dict(os.environ, env, clear=True)
        replacement.start()
        self.addCleanup(replacement.stop)

    def test_missing_credentials_are_rejected(self):
        del os.environ["GH_TOKEN"]
        with self.assertRaisesRegex(ValueError, "GH_TOKEN"):
            Config.from_env()

    def test_validation_is_required_for_every_repository(self):
        self.path.write_text('{"example/repo": {}}')
        with self.assertRaisesRegex(ValueError, "validation"):
            Config.from_env()

    def test_shell_command_strings_are_rejected(self):
        self.path.write_text('{"example/repo": {"validation": ["python -m unittest"]}}')
        with self.assertRaises(ValueError):
            Config.from_env()

    def test_queue_url_cannot_embed_credentials(self):
        os.environ["NQ_URL"] = "https://secret@example.com"
        with self.assertRaises(ValueError):
            Config.from_env()

    def test_unrestricted_execution_window(self):
        self.assertTrue(Config.from_env().window_open())

    def test_overnight_window_uses_explicit_timezone(self):
        os.environ.update(
            NQ_RUNNER_TIMEZONE="America/Los_Angeles",
            NQ_RUNNER_WINDOW_START="22:00",
            NQ_RUNNER_WINDOW_END="06:00",
        )
        config = Config.from_env()
        self.assertTrue(
            config.window_open(datetime(2026, 11, 14, 7, tzinfo=timezone.utc))
        )
        self.assertFalse(
            config.window_open(datetime(2026, 11, 14, 20, tzinfo=timezone.utc))
        )

    def test_daytime_window_excludes_end_boundary(self):
        os.environ.update(NQ_RUNNER_WINDOW_START="08:00", NQ_RUNNER_WINDOW_END="17:00")
        config = Config.from_env()
        self.assertTrue(
            config.window_open(datetime(2026, 11, 14, 8, tzinfo=timezone.utc))
        )
        self.assertFalse(
            config.window_open(datetime(2026, 11, 14, 17, tzinfo=timezone.utc))
        )

    def test_incomplete_window_is_rejected(self):
        os.environ["NQ_RUNNER_WINDOW_START"] = "22:00"
        with self.assertRaises(ValueError):
            Config.from_env()

    def test_nonpositive_timeout_is_rejected(self):
        os.environ["NQ_RUNNER_TIMEOUT_SECONDS"] = "0"
        with self.assertRaises(ValueError):
            Config.from_env()

    def test_repository_urls_are_normalized(self):
        self.assertEqual(
            repository_name("https://github.com/Example/Repo.git"), "example/repo"
        )

    def test_repository_url_cannot_select_other_hosts_or_transports(self):
        for url in [
            "file:///tmp/repo",
            "https://github.com.evil.test/a/b",
            "https://token@github.com/a/b",
            "https://github.com/../repo",
        ]:
            with self.subTest(url=url), self.assertRaises(ValueError):
                repository_name(url)
