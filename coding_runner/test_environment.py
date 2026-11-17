import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from vibearr_runner.environment import getenv


class RestoredRunnerEnvironmentTests(unittest.TestCase):
    def test_restored_token_is_fenced_until_new_identity_bootstrap(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            token = root / "api-token"
            token.write_text("private-restored-token")
            token.chmod(0o600)
            (root / "restore-required.json").write_text("{}")
            with patch.dict(
                os.environ, {"VIBEARR_RUNNER_API_TOKEN_FILE": str(token)}, clear=True
            ):
                with self.assertRaisesRegex(ValueError, "fresh Docker runner identity"):
                    getenv("NQ_RUNNER_API_TOKEN")
