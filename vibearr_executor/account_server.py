"""Protected managed Codex login service; no candidate code or engine access."""

import fcntl
import hmac
from http.server import BaseHTTPRequestHandler
import json
import os
from pathlib import Path
import re
import signal
import threading

from .account_leases import AccountLeases
from .codex_account import private_bytes
from .codex_account_rpc import refresh_account
from .contracts import canonical
from .slot_http import SlotServer


class AccountServer(SlotServer):
    accounts: AccountLeases


class AccountHandler(BaseHTTPRequestHandler):
    server: AccountServer
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):
        pass

    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def reply(self, status, value):
        content = canonical(value)
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(content)

    def do_GET(self):
        if self.path != "/health":
            self.reply(404, {"error": "Unknown account endpoint."})
            return
        self.reply(200, self.server.accounts.healthy())

    def do_POST(self):
        try:
            self.dispatch()
        except (ValueError, TypeError, KeyError, UnicodeError, RecursionError):
            self.reply(
                400,
                {
                    "error": "Account sign-in or operation binding is unavailable; use the protected Docker login command."
                },
            )
        except (OSError, TimeoutError, RuntimeError):
            self.reply(
                503,
                {
                    "error": "Account control could not complete; retain the original operation and private account cache."
                },
            )

    def dispatch(self):
        supplied = self.headers.get("Authorization", "")
        if self.path in {
            "/v1/prepare",
            "/v1/accounts/register",
        } and not hmac.compare_digest(
            supplied.encode(), ("Bearer " + self.server.token).encode()
        ):
            self.reply(401, {"error": "Account control authentication required."})
            return
        length = int(self.headers.get("Content-Length", "0"))
        if not 0 < length <= 65536 or self.headers.get("Transfer-Encoding"):
            raise ValueError("Invalid bounded account request.")
        content = self.rfile.read(length)
        if len(content) != length:
            raise ValueError("Truncated account request.")
        data = json.loads(content)
        if not isinstance(data, dict):
            raise ValueError("Supply an account request object.")
        if self.path == "/v1/accounts/register":
            if set(data) != {"credential", "secret"} or not isinstance(
                data["secret"], str
            ):
                raise ValueError("Select a saved managed account connection.")
            self.reply(
                200, self.server.accounts.register(data["credential"], data["secret"])
            )
            return
        if self.path == "/v1/prepare":
            self.reply(200, self.server.accounts.prepare(data))
            return
        match = re.fullmatch(r"/v1/lease/([a-f0-9-]{36})/(renew|close)", self.path)
        if match is None or not supplied.startswith("Bearer "):
            self.reply(404, {"error": "Unknown account endpoint."})
            return
        if match[2] == "renew":
            if set(data) != {"execution_identity", "request_id"}:
                raise ValueError("Bind the account renewal request.")
            value = self.server.accounts.renew(
                match[1], supplied[7:], data["execution_identity"], data["request_id"]
            )
        else:
            if set(data) != {"execution_identity"}:
                raise ValueError("Bind the account close request.")
            value = self.server.accounts.close(
                match[1], supplied[7:], data["execution_identity"]
            )
        self.reply(200, value)


def main():
    os.umask(0o077)
    root = Path("/var/lib/codex-account")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(
        root / "service.lock", os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600
    )
    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    token = private_bytes(Path("/run/codex-account/control-token")).decode().strip()
    if len(token) < 32:
        raise ValueError("Initialize the private account control token first.")
    accounts = AccountLeases(
        root, lambda home, force: refresh_account(home, force=force, timeout=35)
    )
    stopping = threading.Event()
    with AccountServer(("0.0.0.0", 8793), AccountHandler) as server:
        server.accounts, server.token = accounts, token

        def stop(_signal, _frame):
            stopping.set()

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        try:
            while not stopping.is_set():
                server.handle_request()
        finally:
            os.close(descriptor)


if __name__ == "__main__":
    main()
