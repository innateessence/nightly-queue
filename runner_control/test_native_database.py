import json
import os
from pathlib import Path
import runpy
import tempfile
import unittest
from unittest.mock import Mock, patch

from vibearr_executor import native_task


class NativeDatabaseEnvironmentTests(unittest.TestCase):
    def test_native_task_database_reaches_application_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            context = root / "context.json"
            context.write_text(
                json.dumps({"adapter": "opencode", "model": "qwen3", "prompt": "task"})
            )
            connection = root / "connection.json"
            connection.write_text(json.dumps({"provider": "opencode"}))
            environment = {
                "HOME": directory,
                "VIBEARR_CONTEXT": str(context),
                "VIBEARR_BACKEND_SECRET_FILE": str(connection),
                "DATABASE_URL": "postgresql://task:fixture-password@127.0.0.2:6543/task",
            }
            process = Mock()
            process.wait.return_value = 0
            with (
                patch.dict(os.environ, environment, clear=True),
                patch.object(native_task, "command", return_value=["coding-cli"]),
                patch.object(native_task.subprocess, "Popen", return_value=process),
            ):
                self.assertEqual(native_task.main(), 0)
                settings_path = (
                    Path(__file__).resolve().parents[1] / "vibearr_app/settings.py"
                )
                database = runpy.run_path(str(settings_path))["DATABASES"]["default"]
            self.assertEqual(database["HOST"], "127.0.0.2")
            self.assertEqual(database["PORT"], "6543")
            self.assertEqual(database["NAME"], "task")
            self.assertEqual(database["USER"], "task")
            self.assertEqual(database["PASSWORD"], "fixture-password")
