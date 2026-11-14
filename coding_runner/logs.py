import queue
import threading
import time

from .http import APIError


class Redactor:
    def __init__(self, secrets):
        self.secrets = secrets
        self.pending = ""

    def feed(self, text, final=False):
        self.pending += text
        keep = max((len(secret) for secret in self.secrets if secret), default=1) - 1
        cut = len(self.pending) if final else max(0, len(self.pending) - keep)
        for secret in self.secrets:
            if not secret:
                continue
            position = self.pending.find(secret)
            while position >= 0:
                if position < cut < position + len(secret):
                    cut = position
                position = self.pending.find(secret, position + len(secret))
        result = self.pending[:cut]
        self.pending = self.pending[cut:]
        for secret in sorted(self.secrets, key=len, reverse=True):
            if secret:
                result = result.replace(secret, "[REDACTED]")
        return result


class WebLogs:
    def __init__(self, client, item, run_id):
        self.client = client
        self.item = item
        self.run_id = run_id
        self.messages = queue.Queue(maxsize=256)
        self.stopping = threading.Event()
        self.sequence = 0
        self.truncated = False
        self.failed = False
        self.close_deadline = float("inf")
        self.thread = threading.Thread(target=self.publish, daemon=True)
        self.thread.start()

    def emit(self, text):
        if not text or self.stopping.is_set() or self.truncated:
            return
        for start in range(0, len(text), 4096):
            try:
                self.messages.put_nowait(text[start : start + 4096])
            except queue.Full:
                self.truncated = True
                return

    def send(self, text):
        self.sequence += 1
        if self.sequence == 256:
            text = "\nWeb log limit reached. Full command output is retained by the runner.\n"
            self.truncated = True
        for attempt in range(2):
            if time.monotonic() >= self.close_deadline:
                self.failed = True
                return
            try:
                self.client.log(self.item, self.run_id, self.sequence, text)
                return
            except APIError:
                pass
        self.failed = True
        # Logging must not fail a coding task or its completion report.

    def publish(self):
        pending = ""
        deadline = time.monotonic() + 2
        try:
            while self.sequence < 256 and time.monotonic() < self.close_deadline:
                try:
                    pending += self.messages.get(timeout=0.1)
                except queue.Empty:
                    pass
                if len(pending) >= 4096 or (pending and time.monotonic() >= deadline):
                    self.send(pending[:4096])
                    pending = pending[4096:]
                    deadline = time.monotonic() + 2
                if self.stopping.is_set() and self.messages.empty():
                    while (
                        pending
                        and self.sequence < 256
                        and time.monotonic() < self.close_deadline
                    ):
                        self.send(pending[:4096])
                        pending = pending[4096:]
                    if self.truncated and self.sequence < 256:
                        self.send(
                            "\nWeb log buffer filled. Full output is retained by the runner.\n"
                        )
                    return
            self.failed = self.failed or bool(pending) or not self.messages.empty()
        except Exception:
            self.failed = True

    def close(self):
        self.stopping.set()
        self.close_deadline = time.monotonic() + 3.5
        self.thread.join(timeout=7)
        if self.failed or self.thread.is_alive():
            print(
                "Web log upload incomplete; private runner logs are retained.",
                flush=True,
            )
