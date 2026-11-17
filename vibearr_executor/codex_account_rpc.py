"""Bounded access to the pinned CLI's managed account refresh."""

import json
import os
from pathlib import Path
import selectors
import signal
import subprocess
import time
from typing import Any

from .codex_account import OAUTH_ISSUER
from .codex_task import BINARY, VERSION


class AccountRPC:
    def __init__(self, home: Path, *, binary: str = BINARY, timeout: float = 60):
        self.home, self.binary, self.timeout = Path(home), binary, timeout
        self.process: subprocess.Popen[bytes] | None = None
        self.selector = selectors.DefaultSelector()
        self.pending = b""
        self.received = 0
        self.deadline = 0.0

    def __enter__(self) -> "AccountRPC":
        environment = {
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "LANG": "C.UTF-8",
            "HOME": str(self.home.parent),
            "CODEX_HOME": str(self.home),
            "CODEX_REFRESH_TOKEN_URL_OVERRIDE": OAUTH_ISSUER + "/oauth/token",
        }
        for name in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy"):
            if name in os.environ:
                environment[name] = os.environ[name]
        arguments = [
            self.binary,
            "-c",
            'cli_auth_credentials_store="file"',
            "-c",
            'forced_login_method="chatgpt"',
            "-c",
            "cloud.skills.enabled=false",
            "-c",
            "plugins._default.enabled=false",
            "-c",
            "analytics.enabled=false",
            "app-server",
            "--stdio",
        ]
        self.deadline = time.monotonic() + self.timeout
        try:
            version = subprocess.run(
                [self.binary, "--version"],
                cwd=self.home,
                env=environment,
                capture_output=True,
                timeout=5,
            )
            if (
                version.returncode
                or version.stdout.strip() != ("codex-cli " + VERSION).encode()
            ):
                raise ValueError(
                    "Account authentication requires the pinned Codex CLI."
                )
            self.process = subprocess.Popen(
                arguments,
                cwd=self.home,
                env=environment,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
            assert self.process.stdout is not None
            assert self.process.stderr is not None
            self.selector.register(self.process.stdout, selectors.EVENT_READ, "stdout")
            self.selector.register(self.process.stderr, selectors.EVENT_READ, "stderr")
        except BaseException:
            self.__exit__()
            raise
        return self

    def __exit__(self, *args: Any) -> None:
        self.selector.close()
        if self.process is None:
            return
        try:
            os.killpg(self.process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            self.process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass
        try:
            os.killpg(self.process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        self.process.wait(timeout=2)
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            if stream is not None:
                stream.close()

    def send(self, value: dict) -> None:
        if self.process is None or self.process.stdin is None:
            raise RuntimeError("Account authentication process is unavailable.")
        self.process.stdin.write(
            json.dumps(value, separators=(",", ":")).encode() + b"\n"
        )
        self.process.stdin.flush()

    def request(self, identity: int, method: str, params: dict) -> dict:
        self.send({"id": identity, "method": method, "params": params})
        while time.monotonic() < self.deadline:
            while b"\n" in self.pending:
                line, self.pending = self.pending.split(b"\n", 1)
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise ValueError("Invalid account authentication response.")
                if value.get("id") != identity:
                    continue
                if "error" in value or not isinstance(value.get("result"), dict):
                    raise ValueError(
                        "Codex account login needs attention; sign in again."
                    )
                return value["result"]
            remaining = max(0, self.deadline - time.monotonic())
            ready = self.selector.select(min(remaining, 1))
            for selected, _ in ready:
                content = os.read(selected.fd, 65536)
                self.received += len(content)
                if self.received > 1024**2:
                    raise ValueError(
                        "Account authentication output exceeded its limit."
                    )
                if not content:
                    self.selector.unregister(selected.fileobj)
                    if selected.data == "stdout":
                        raise ValueError(
                            "Codex account authentication exited before completion."
                        )
                elif selected.data == "stdout":
                    self.pending += content
        raise TimeoutError(
            "Codex account authentication did not finish within its deadline."
        )


def refresh_account(home: Path, *, force: bool = False) -> None:
    with AccountRPC(home) as client:
        client.request(
            1,
            "initialize",
            {"clientInfo": {"name": "vibearr-account", "version": VERSION}},
        )
        client.send({"method": "initialized", "params": {}})
        result = client.request(2, "account/read", {"refreshToken": force})
        account = result.get("account")
        if not isinstance(account, dict) or account.get("type") != "chatgpt":
            raise ValueError("Sign in to the selected Codex account in Docker.")
