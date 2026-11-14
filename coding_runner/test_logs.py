import tempfile
import threading
import unittest
from pathlib import Path

from .http import APIError
from .logs import Redactor, WebLogs
from .process import Commands, clean_environment


class Client:
    def __init__(self):
        self.events = []

    def log(self, item, run_id, sequence, text):
        self.events.append((sequence, text))


class LogTests(unittest.TestCase):
    def test_secret_split_across_stream_chunks_is_never_emitted(self):
        redactor = Redactor(["private-api-key"])
        output = redactor.feed("Starting private-api-")
        output += redactor.feed("key done")
        output += redactor.feed("", final=True)
        self.assertEqual(output, "Starting [REDACTED] done")

    def test_multiple_and_overlapping_secrets_are_redacted(self):
        redactor = Redactor(["secret", "long-secret-value"])
        output = "".join(redactor.feed(char) for char in "long-secret-value secret")
        output += redactor.feed("", final=True)
        self.assertNotIn("secret", output)
        self.assertNotIn("value", output)

    def test_web_upload_preserves_large_chunks_without_exceeding_request_limit(self):
        client = Client()
        logs = WebLogs(client, {"id": 1}, "run-id")
        text = "x" * 9000
        logs.emit(text)
        logs.close()
        self.assertEqual("".join(text for _, text in client.events), text)
        self.assertTrue(all(len(text) <= 4096 for _, text in client.events))

    def test_transient_log_errors_retry_the_same_sequence(self):
        class RetryClient(Client):
            def log(self, item, run_id, sequence, text):
                super().log(item, run_id, sequence, text)
                if len(self.events) == 1:
                    raise APIError("Response lost")

        client = RetryClient()
        logs = WebLogs(client, {"id": 1}, "run-id")
        logs.emit("Progress")
        logs.close()
        self.assertEqual(client.events, [(1, "Progress"), (1, "Progress")])

    def test_log_upload_failure_does_not_raise_into_task_execution(self):
        class BrokenClient:
            def log(self, *args):
                raise APIError("Unavailable")

        logs = WebLogs(BrokenClient(), {"id": 1}, "run-id")
        logs.emit("Progress")
        logs.close()
        self.assertTrue(logs.failed)

    def test_running_command_publishes_output_before_exit(self):
        import sys

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            observed = threading.Event()
            release = root / "release"
            commands = Commands(
                root / "commands.log", 5, on_output=lambda text: observed.set()
            )
            script = "import time; from pathlib import Path; print('live output',flush=True)\nwhile not Path('release').exists(): time.sleep(.05)"
            errors = []

            def run():
                try:
                    commands.run(
                        [sys.executable, "-c", script], root, clean_environment(root)
                    )
                except Exception as exc:
                    errors.append(exc)

            thread = threading.Thread(target=run)
            thread.start()
            try:
                self.assertTrue(observed.wait(timeout=2))
                self.assertTrue(thread.is_alive())
            finally:
                release.touch()
                thread.join(timeout=6)
            self.assertFalse(errors)
